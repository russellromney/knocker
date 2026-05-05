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
      @endpoint_configs = {}
      exec("SELECT knocker_bootstrap()")
    end

    def close
      SQLite.sqlite3_close(@db)
      nil
    end

    def add_endpoint(name:, path:, provider: nil, enabled: true, secrets: [], provider_options: {})
      id = scalar(
        "SELECT knocker_endpoint_upsert(#{sql_literal(name)}, #{sql_literal(path)}, #{sql_literal(provider)}, #{enabled ? 1 : 0})"
      ).to_i
      @endpoint_configs[name.to_s] = {
        provider: provider,
        secrets: secrets,
        provider_options: provider_options
      }
      id
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

    def receive(
      endpoint:,
      body:,
      headers: {},
      query: {},
      method: "POST",
      provider: nil,
      secrets: nil,
      provider_options: nil,
      event_type: nil,
      provider_event_id: nil,
      provider_delivery_id: nil,
      dedupe_key: nil,
      queue_name: "knocker.events",
      max_attempts: 3
    )
      config = @endpoint_configs.fetch(endpoint.to_s, {})
      provider_name = provider || config[:provider]
      raise ArgumentError, "endpoint #{endpoint} has no provider configured" unless provider_name

      exec("BEGIN IMMEDIATE")
      result_json =
        scalar(
          "SELECT knocker_receive(" \
            "#{sql_literal(endpoint)}, " \
            "#{sql_literal(provider_name)}, " \
            "#{sql_literal(JSON.dump(secrets || config.fetch(:secrets, [])))}, " \
            "#{sql_literal(JSON.dump(provider_options || config.fetch(:provider_options, {})))}, " \
            "#{sql_literal(method)}, " \
            "#{sql_literal(JSON.dump(headers))}, " \
            "#{blob_literal(body)}, " \
            "#{sql_literal(JSON.dump(query))}, " \
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

    def get_delivery(delivery_id)
      json =
        scalar(
          "SELECT json_object(" \
            "'id', d.id, " \
            "'event_id', d.event_id, " \
            "'endpoint', ep.name, " \
            "'event_type', d.event_type, " \
            "'provider_event_id', d.provider_event_id, " \
            "'provider_delivery_id', d.provider_delivery_id, " \
            "'dedupe_key', d.dedupe_key, " \
            "'method', d.method, " \
            "'headers_json', d.headers_json, " \
            "'query_json', d.query_json, " \
            "'body_blob', CAST(d.body_blob AS TEXT), " \
            "'received_at', d.received_at, " \
            "'signature_valid', d.signature_valid, " \
            "'signature_error', d.signature_error" \
          ") " \
          "FROM knocker_deliveries d " \
          "JOIN knocker_endpoints ep ON ep.id = d.endpoint_id " \
          "WHERE d.id=#{Integer(delivery_id)}"
        )
      JSON.parse(json)
    end

    def list_deliveries(event_id: nil, endpoint: nil, signature_valid: nil, orphaned: nil, since: nil, limit: 100)
      clauses = []
      clauses << "d.event_id=#{Integer(event_id)}" unless event_id.nil?
      clauses << "ep.name=#{sql_literal(endpoint)}" unless endpoint.nil?
      unless signature_valid.nil?
        clauses << (signature_valid ? "d.signature_valid=1" : "(d.signature_valid=0 OR d.signature_valid IS NULL)")
      end
      unless orphaned.nil?
        clauses << (orphaned ? "d.event_id IS NULL" : "d.event_id IS NOT NULL")
      end
      clauses << "d.received_at>=#{Integer(since)}" unless since.nil?
      where = clauses.empty? ? "" : "WHERE #{clauses.join(' AND ')}"
      json =
        scalar(
          "SELECT COALESCE(json_group_array(json_object(" \
            "'id', id, " \
            "'event_id', event_id, " \
            "'endpoint', endpoint, " \
            "'event_type', event_type, " \
            "'provider_event_id', provider_event_id, " \
            "'provider_delivery_id', provider_delivery_id, " \
            "'dedupe_key', dedupe_key, " \
            "'method', method, " \
            "'headers_json', headers_json, " \
            "'query_json', query_json, " \
            "'body_blob', body_blob, " \
            "'received_at', received_at, " \
            "'signature_valid', signature_valid, " \
            "'signature_error', signature_error" \
          ")), '[]') " \
          "FROM (" \
            "SELECT d.id AS id, d.event_id AS event_id, ep.name AS endpoint, " \
              "d.event_type AS event_type, d.provider_event_id AS provider_event_id, " \
              "d.provider_delivery_id AS provider_delivery_id, d.dedupe_key AS dedupe_key, " \
              "d.method AS method, d.headers_json AS headers_json, d.query_json AS query_json, " \
              "CAST(d.body_blob AS TEXT) AS body_blob, d.received_at AS received_at, " \
              "d.signature_valid AS signature_valid, d.signature_error AS signature_error " \
            "FROM knocker_deliveries d " \
            "JOIN knocker_endpoints ep ON ep.id = d.endpoint_id " \
            "#{where} ORDER BY d.received_at DESC, d.id DESC LIMIT #{Integer(limit)}" \
          ")"
        )
      JSON.parse(json)
    end

    def replay(event_id)
      scalar("SELECT knocker_replay(#{Integer(event_id)}, 'knocker.events', 3)")
    end

    def requeue(event_id)
      scalar("SELECT knocker_requeue(#{Integer(event_id)}, 'knocker.events', 3)")
    end

    def ignore(event_id)
      exec("BEGIN IMMEDIATE")
      event = get_event(event_id)
      if event["status"] != "ignored"
        unless %w[received failed dead].include?(event["status"])
          raise "event #{event_id} with status #{event['status']} cannot be ignored"
        end
        scalar("SELECT knocker_mark_ignored(#{Integer(event_id)}, 0)")
      end
      exec("COMMIT")
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

    def replay_delivery(delivery_id)
      exec("BEGIN IMMEDIATE")
      delivery = get_delivery(delivery_id)
      raise "delivery #{delivery_id} is not linked to an event" if delivery["event_id"].nil?

      event = get_event(delivery["event_id"])
      unless %w[handled failed dead ignored].include?(event["status"])
        raise "event #{delivery['event_id']} with status #{event['status']} cannot replay a delivery"
      end
      _delete_live_jobs_for_event(delivery["event_id"])
      scalar("SELECT knocker_reset_event(#{Integer(delivery['event_id'])})")
      payload = JSON.dump({ event_id: delivery["event_id"], delivery_id: delivery["id"] })
      scalar("SELECT honker_enqueue('knocker.events', #{sql_literal(payload)}, NULL, NULL, 0, 3, NULL)")
      exec("COMMIT")
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

    def prune_events(statuses:, older_than:, limit:)
      exec("BEGIN IMMEDIATE")
      result = JSON.parse(
        scalar(
          "SELECT knocker_prune_events(#{sql_literal(JSON.dump(statuses))}, #{Integer(older_than)}, #{Integer(limit)}, 'knocker.events')"
        )
      )
      exec("COMMIT")
      result
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

    def prune_orphan_deliveries(older_than:, limit:)
      exec("BEGIN IMMEDIATE")
      result = JSON.parse(
        scalar("SELECT knocker_prune_orphan_deliveries(#{Integer(older_than)}, #{Integer(limit)}, 'knocker.events')")
      )
      exec("COMMIT")
      result
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

    def list_prune_audits(kind: nil, since: nil, limit: 50)
      clauses = []
      clauses << "kind=#{sql_literal(kind)}" unless kind.nil?
      clauses << "executed_at>=#{Integer(since)}" unless since.nil?
      where = clauses.empty? ? "" : "WHERE #{clauses.join(' AND ')}"
      JSON.parse(
        scalar(
          "SELECT COALESCE(json_group_array(json_object(" \
            "'id', id, 'kind', kind, 'queue_name', queue_name, 'executed_at', executed_at, " \
            "'events_pruned', events_pruned, 'deliveries_pruned', deliveries_pruned, " \
            "'attempts_pruned', attempts_pruned, 'live_jobs_pruned', live_jobs_pruned, " \
            "'summary_json', summary_json)), '[]') " \
          "FROM (SELECT * FROM knocker_prune_audits #{where} ORDER BY executed_at DESC, id DESC LIMIT #{Integer(limit)})"
        )
      )
    end

    def run_retention_once(
      statuses: %w[handled ignored],
      event_older_than_s: nil,
      event_limit: 1000,
      orphan_deliveries_older_than_s: nil,
      orphan_deliveries_limit: 1000,
      queue_name: "knocker.events",
      now: Time.now.to_i
    )
      event_cutoff = event_older_than_s.nil? ? "NULL" : Integer(now - event_older_than_s)
      orphan_cutoff = orphan_deliveries_older_than_s.nil? ? "NULL" : Integer(now - orphan_deliveries_older_than_s)
      exec("BEGIN IMMEDIATE")
      result = JSON.parse(
        scalar(
          "SELECT knocker_run_retention_pass(" \
            "#{sql_literal(JSON.dump(statuses))}, #{event_cutoff}, #{Integer(event_limit)}, " \
            "#{orphan_cutoff}, #{Integer(orphan_deliveries_limit)}, #{sql_literal(queue_name)})"
        )
      )
      exec("COMMIT")
      result
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

    def run_retention(interval_s: 60, max_runs: nil, should_stop: nil, **policy)
      runs = 0
      while max_runs.nil? || runs < max_runs
        break if should_stop&.call

        run_retention_once(**policy)
        runs += 1
        break if !max_runs.nil? && runs >= max_runs

        sleep(interval_s)
      end
      runs
    end

    def register_handler(endpoint, event_type: nil, &block)
      raise ArgumentError, "block required" unless block

      @handlers[handler_key(endpoint, event_type)] = block
      self
    end

    def run_worker_once(worker_id: "ruby-worker")
      rows = JSON.parse(scalar("SELECT honker_claim_batch('knocker.events', #{sql_literal(worker_id)}, 1, 60)"))
      return nil if rows.empty?
      rows.first["max_attempts"] ||= scalar("SELECT max_attempts FROM _honker_live WHERE id=#{Integer(rows.first.fetch('id'))}").to_i

      payload = JSON.parse(rows.first.fetch("payload"))
      event_id = Integer(payload.fetch("event_id"))
      event = get_event(event_id)
      if payload["delivery_id"]
        delivery = get_delivery(payload["delivery_id"])
        event = event.merge(
          "event_type" => delivery["event_type"],
          "provider_event_id" => delivery["provider_event_id"],
          "provider_delivery_id" => delivery["provider_delivery_id"],
          "dedupe_key" => delivery["dedupe_key"],
          "body_blob" => delivery["body_blob"]
        )
      end
      handler =
        @handlers[handler_key(event.fetch("endpoint"), event["event_type"])] ||
        @handlers[handler_key(event.fetch("endpoint"), nil)]
      unless handler
        error = RuntimeError.new("no handler registered for endpoint #{event.fetch('endpoint').inspect}")
        _fail_claim(rows.first, worker_id, event_id, error.message)
        raise error
      end
      begin
        exec("BEGIN IMMEDIATE")
        scalar("SELECT knocker_mark_processing(#{event_id}, #{Integer(rows.first.fetch('attempts'))})")
        handler.call(event, Transaction.new(self))
        scalar("SELECT knocker_mark_handled(#{event_id}, 0)")
        scalar("SELECT honker_ack(#{Integer(rows.first.fetch('id'))}, #{sql_literal(worker_id)})")
        exec("COMMIT")
      rescue StandardError
        message = $!.message
        exec("ROLLBACK")
        _record_failed_claim(rows.first, worker_id, event_id, message)
        raise
      end
      event_id
    end

    def run_worker(worker_id: "ruby-worker", idle_poll_s: 0.1, max_jobs: nil, should_stop: nil, on_error: nil)
      processed = 0
      while max_jobs.nil? || processed < max_jobs
        break if should_stop&.call

        begin
          event_id = run_worker_once(worker_id: worker_id)
          if event_id.nil?
            sleep(idle_poll_s)
            next
          end
          processed += 1
        rescue StandardError => e
          raise unless on_error

          on_error.call(e)
        end
      end
      processed
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

    class Transaction
      def initialize(db)
        @db = db
      end

      def exec(sql)
        @db.send(:exec, sql)
      end

      def scalar(sql)
        @db.send(:scalar, sql)
      end
    end

    private

    def handler_key(endpoint, event_type)
      "#{endpoint}\0#{event_type}"
    end

    def _record_failed_claim(job, worker_id, event_id, message)
      attempt_count = Integer(job.fetch("attempts"))
      terminal = attempt_count >= Integer(job.fetch("max_attempts"))
      exec("BEGIN IMMEDIATE")
      scalar(
        "SELECT knocker_mark_failed(#{Integer(event_id)}, #{attempt_count}, " \
        "#{sql_literal(message)}, #{terminal ? 1 : 0}, 0)"
      )
      if terminal
        scalar("SELECT honker_fail(#{Integer(job.fetch('id'))}, #{sql_literal(worker_id)}, #{sql_literal(message)})")
      else
        scalar("SELECT honker_retry(#{Integer(job.fetch('id'))}, #{sql_literal(worker_id)}, 0, #{sql_literal(message)})")
      end
      exec("COMMIT")
    rescue StandardError
      exec("ROLLBACK")
      raise
    end

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

    def _delete_live_jobs_for_event(event_id)
      rows = JSON.parse(
        scalar(
          "SELECT COALESCE(json_group_array(json_object('id', id, 'payload', payload)), '[]') " \
          "FROM _honker_live WHERE queue='knocker.events'"
        )
      )
      rows.each do |row|
        payload = JSON.parse(row["payload"])
        next unless Integer(payload["event_id"]) == Integer(event_id)

        exec("DELETE FROM _honker_live WHERE id=#{Integer(row['id'])}")
      rescue JSON::ParserError, TypeError
        next
      end
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
