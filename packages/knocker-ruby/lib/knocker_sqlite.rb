require "fiddle/import"
require "json"
require "rbconfig"

module KnockerSqlite
  SQLITE_CANDIDATES = [
    ENV["KNOCKER_SQLITE3_LIB"],
    ENV["LIBSQLITE3_PATH"],
    "/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib",
    "/usr/local/opt/sqlite/lib/libsqlite3.dylib",
    "/usr/lib/libsqlite3.dylib",
    "/usr/lib/x86_64-linux-gnu/libsqlite3.so",
    "/usr/lib/aarch64-linux-gnu/libsqlite3.so",
    "/usr/lib64/libsqlite3.so",
    "/usr/lib/libsqlite3.so"
  ].compact.freeze

  module SQLite
    extend Fiddle::Importer

    dlload(*KnockerSqlite::SQLITE_CANDIDATES.select { |path| File.exist?(path) })

    extern "int sqlite3_open(const char*, void*)"
    extern "int sqlite3_close(void*)"
    extern "int sqlite3_enable_load_extension(void*, int)"
    extern "int sqlite3_load_extension(void*, const char*, const char*, void*)"
    extern "char* sqlite3_errmsg(void*)"
    extern "int sqlite3_exec(void*, const char*, void*, void*, void*)"
    extern "int sqlite3_prepare_v2(void*, const char*, int, void*, void*)"
    extern "int sqlite3_step(void*)"
    extern "char* sqlite3_column_text(void*, int)"
    extern "int sqlite3_finalize(void*)"
    extern "void sqlite3_free(void*)"

    SQLITE_ROW = 100
    SQLITE_DONE = 101
  end

  class Database
    def self.open(path)
      new(path)
    end

    def initialize(path)
      @db = _sqlite3_open(path)
      _check_rc(SQLite.sqlite3_enable_load_extension(@db, 1))
      _check_rc(
        SQLite.sqlite3_load_extension(
          @db,
          self.class.extension_path,
          "sqlite3_knockerext_init",
          _pointer_out_buffer
        )
      )
      _check_rc(SQLite.sqlite3_enable_load_extension(@db, 0))
      @handlers = {}
      exec("SELECT knocker_bootstrap()")
    end

    def close
      SQLite.sqlite3_close(@db)
      nil
    end

    def add_endpoint(name:, path:, provider: nil, enabled: true)
      scalar(
        "SELECT knocker_endpoint_upsert(#{sql_literal(name)}, #{sql_literal(path)}, #{sql_literal(provider)}, #{enabled ? 1 : 0})"
      ).to_i
    end

    def ingest(
      endpoint:,
      body:,
      headers: {},
      query: {},
      method: "POST",
      event_type: nil,
      provider_event_id: nil,
      provider_delivery_id: nil,
      dedupe_key: nil,
      signature_valid: true,
      signature_error: nil,
      queue_name: "knocker.events",
      max_attempts: 3
    )
      signature_sql =
        if signature_valid.nil?
          "NULL"
        else
          signature_valid ? "1" : "0"
        end

      exec("BEGIN IMMEDIATE")
      result_json =
        scalar(
          "SELECT knocker_ingest(" \
            "#{sql_literal(endpoint)}, " \
            "#{sql_literal(method)}, " \
            "#{sql_literal(JSON.dump(headers))}, " \
            "#{blob_literal(body)}, " \
            "#{sql_literal(JSON.dump(query))}, " \
            "#{signature_sql}, " \
            "#{sql_literal(signature_error)}, " \
            "#{sql_literal(provider_event_id)}, " \
            "#{sql_literal(provider_delivery_id)}, " \
            "#{sql_literal(event_type)}, " \
            "#{sql_literal(dedupe_key)}, " \
            "#{sql_literal(queue_name)}, " \
            "#{Integer(max_attempts)})"
        )
      exec("COMMIT")
      JSON.parse(result_json)
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

    def get_event(event_id)
      json =
        scalar(
          "SELECT json_object(" \
            "'id', e.id, " \
            "'endpoint', ep.name, " \
            "'event_type', e.event_type, " \
            "'provider_event_id', e.provider_event_id, " \
            "'provider_delivery_id', e.provider_delivery_id, " \
            "'status', e.status, " \
            "'attempt_count', e.attempt_count, " \
            "'body_blob', CAST(e.body_blob AS TEXT)" \
          ") " \
          "FROM knocker_events e " \
          "JOIN knocker_endpoints ep ON ep.id = e.endpoint_id " \
          "WHERE e.id=#{Integer(event_id)}"
        )
      JSON.parse(json)
    end

    def list_events(limit: 50)
      json =
        scalar(
          "SELECT COALESCE(json_group_array(json_object(" \
            "'id', id, " \
            "'endpoint', endpoint, " \
            "'event_type', event_type, " \
            "'provider_event_id', provider_event_id, " \
            "'provider_delivery_id', provider_delivery_id, " \
            "'status', status, " \
            "'attempt_count', attempt_count" \
          ")), '[]') " \
          "FROM (" \
            "SELECT e.id AS id, ep.name AS endpoint, e.event_type AS event_type, " \
              "e.provider_event_id AS provider_event_id, e.provider_delivery_id AS provider_delivery_id, " \
              "e.status AS status, e.attempt_count AS attempt_count " \
            "FROM knocker_events e " \
            "JOIN knocker_endpoints ep ON ep.id = e.endpoint_id " \
            "ORDER BY e.id LIMIT #{Integer(limit)}" \
          ")"
        )
      JSON.parse(json)
    end

    def replay(event_id)
      scalar("SELECT knocker_replay(#{Integer(event_id)}, 'knocker.events', 3)")
    end

    def register_handler(endpoint, &block)
      raise ArgumentError, "block required" unless block

      @handlers[endpoint.to_s] = block
      self
    end

    def run_worker_once(worker_id: "ruby-worker")
      rows = JSON.parse(scalar("SELECT honker_claim_batch('knocker.events', #{sql_literal(worker_id)}, 1, 60)"))
      return nil if rows.empty?

      payload = JSON.parse(rows.first.fetch("payload"))
      event_id = Integer(payload.fetch("event_id"))
      event = get_event(event_id)
      handler = @handlers[event.fetch("endpoint")]
      unless handler
        error = RuntimeError.new("no handler registered for endpoint #{event.fetch('endpoint').inspect}")
        _fail_claim(rows.first, worker_id, event_id, error.message)
        raise error
      end
      exec("BEGIN IMMEDIATE")
      scalar("SELECT knocker_mark_processing(#{event_id}, #{Integer(rows.first.fetch('attempts'))})")
      handler.call(event)
      scalar("SELECT knocker_mark_handled(#{event_id}, 0)")
      scalar("SELECT honker_ack(#{Integer(rows.first.fetch('id'))}, #{sql_literal(worker_id)})")
      exec("COMMIT")
      event_id
    rescue StandardError
      begin
        message = $!.message
        terminal = Integer(rows.first.fetch("attempts")) >= Integer(rows.first.fetch("max_attempts"))
        scalar(
          "SELECT knocker_mark_failed(#{event_id}, #{Integer(rows.first.fetch('attempts'))}, " \
          "#{sql_literal(message)}, #{terminal ? 1 : 0}, 0)"
        )
        if terminal
          scalar("SELECT honker_fail(#{Integer(rows.first.fetch('id'))}, #{sql_literal(worker_id)}, #{sql_literal(message)})")
        else
          scalar("SELECT honker_retry(#{Integer(rows.first.fetch('id'))}, #{sql_literal(worker_id)}, 0, #{sql_literal(message)})")
        end
        exec("COMMIT")
      rescue StandardError
        exec("ROLLBACK")
      end
      raise
    end

    def self.extension_path
      ext =
        case RbConfig::CONFIG["host_os"]
        when /darwin/
          "dylib"
        when /mswin|mingw/
          "dll"
        else
          "so"
        end
      File.expand_path("../../../target/release/libknocker_ext.#{ext}", __dir__)
    end

    private

    def exec(sql)
      err = _pointer_out_buffer
      rc = SQLite.sqlite3_exec(@db, sql, 0, 0, err)
      return nil if rc.zero?

      message = _pointer_string(err)
      SQLite.sqlite3_free(_unpack_ptr(err)) unless _unpack_ptr(err).zero?
      raise message.empty? ? _errmsg : message
    end

    def scalar(sql)
      stmt_buf = _pointer_out_buffer
      _check_rc(SQLite.sqlite3_prepare_v2(@db, sql, -1, stmt_buf, 0))
      stmt = _unpack_ptr(stmt_buf)
      rc = SQLite.sqlite3_step(stmt)
      value =
        case rc
        when SQLite::SQLITE_ROW
          pointer = SQLite.sqlite3_column_text(stmt, 0)
          pointer ? pointer.to_s : nil
        when SQLite::SQLITE_DONE
          nil
        else
          raise _errmsg
        end
      SQLite.sqlite3_finalize(stmt)
      value
    end

    def sql_literal(value)
      return "NULL" if value.nil?

      "'#{value.to_s.gsub("'", "''")}'"
    end

    def blob_literal(value)
      "X'#{value.b.unpack1("H*")}'"
    end

    def _sqlite3_open(path)
      buf = _pointer_out_buffer
      _check_rc(SQLite.sqlite3_open(path.to_s, buf))
      _unpack_ptr(buf)
    end

    def _pointer_out_buffer
      Fiddle::Pointer.malloc(Fiddle::SIZEOF_VOIDP).tap do |ptr|
        ptr[0, Fiddle::SIZEOF_VOIDP] = _pack_ptr(0)
      end
    end

    def _unpack_ptr(buffer)
      buffer[0, Fiddle::SIZEOF_VOIDP].unpack1(_pointer_pack_template)
    end

    def _pointer_string(buffer)
      ptr = _unpack_ptr(buffer)
      return "" if ptr.zero?

      Fiddle::Pointer.new(ptr).to_s
    end

    def _check_rc(rc)
      return if rc.zero?

      raise _errmsg
    end

    def _fail_claim(job, worker_id, event_id, message)
      exec("BEGIN IMMEDIATE")
      scalar(
        "SELECT knocker_mark_failed(#{event_id}, #{Integer(job.fetch('attempts'))}, #{sql_literal(message)}, 1, 0)"
      )
      scalar("SELECT honker_fail(#{Integer(job.fetch('id'))}, #{sql_literal(worker_id)}, #{sql_literal(message)})")
      exec("COMMIT")
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

    def _errmsg
      SQLite.sqlite3_errmsg(@db).to_s
    end

    def _pack_ptr(value)
      [value].pack(_pointer_pack_template)
    end

    def _pointer_pack_template
      Fiddle::SIZEOF_VOIDP == 8 ? "Q" : "L"
    end
  end
end
