require "minitest/autorun"
require "tmpdir"
require_relative "../lib/knocker_sqlite"

class KnockerSqliteTest < Minitest::Test
  def test_ruby_binding_end_to_end
    Dir.mktmpdir("knocker-ruby-") do |dir|
      db_path = File.join(dir, "app.db")
      app = KnockerSqlite::Database.open(db_path)
      app.send(:exec, "CREATE TABLE handled_events (event_id INTEGER PRIMARY KEY, body TEXT)")

      app.add_endpoint(name: "github", path: "/webhooks/github", provider: "github", enabled: true)
      result =
        app.ingest(
          endpoint: "github",
          body: "{\"hello\":\"world\"}",
          provider_delivery_id: "ruby-delivery-1",
          signature_valid: true
        )

      refute_nil result["event_id"]
      event = app.get_event(result["event_id"])
      assert_equal "received", event["status"]

      events = app.list_events(limit: 10)
      assert_equal 1, events.length

      handled = []
      app.register_handler("github") do |event, tx|
        handled << event["id"]
        assert_equal "{\"hello\":\"world\"}", event["body_blob"]
        tx.exec("INSERT INTO handled_events (event_id, body) VALUES (#{event['id']}, '{\"hello\":\"world\"}')")
      end

      processed_event_id = app.run_worker_once(worker_id: "ruby-worker")
      assert_equal result["event_id"], processed_event_id
      assert_equal [result["event_id"]], handled

      event = app.get_event(result["event_id"])
      assert_equal "handled", event["status"]
      assert_equal "{\"hello\":\"world\"}", app.send(:scalar, "SELECT body FROM handled_events WHERE event_id=#{result['event_id']}")

      app.replay(result["event_id"])
      event = app.get_event(result["event_id"])
      assert_equal "received", event["status"]

      app.close
    end
  end

  def test_ruby_binding_operator_parity
    Dir.mktmpdir("knocker-ruby-") do |dir|
      db_path = File.join(dir, "app.db")
      app = KnockerSqlite::Database.open(db_path)

      app.add_endpoint(name: "github", path: "/webhooks/github", provider: "github", enabled: true)
      result =
        app.ingest(
          endpoint: "github",
          body: "{\"version\":1}",
          provider_delivery_id: "ruby-delivery-parity",
          event_type: "v1",
          signature_valid: true
        )
      duplicate =
        app.ingest(
          endpoint: "github",
          body: "{\"version\":2}",
          provider_delivery_id: "ruby-delivery-parity",
          event_type: "v2",
          signature_valid: true
        )

      assert_equal 1, duplicate["duplicate"]
      assert_equal 2, app.list_deliveries(event_id: result["event_id"]).length
      assert_equal "ruby-delivery-parity", app.get_delivery(duplicate["delivery_id"])["provider_delivery_id"]

      app.register_handler("github") { |_event| nil }
      app.run_worker_once(worker_id: "ruby-parity-worker")
      assert_equal "handled", app.get_event(result["event_id"])["status"]

      replay_bodies = []
      app.register_handler("github") do |_event|
        raise "replay_delivery used endpoint fallback instead of delivery event type"
      end
      app.register_handler("github", event_type: "v2") do |event|
        replay_bodies << "#{event['event_type']}:#{event['body_blob']}"
      end
      app.replay_delivery(duplicate["delivery_id"])
      app.run_worker_once(worker_id: "ruby-delivery-worker")
      assert_equal ["v2:{\"version\":2}"], replay_bodies

      app.replay(result["event_id"])
      app.ignore(result["event_id"])
      assert_equal "ignored", app.get_event(result["event_id"])["status"]
      app.requeue(result["event_id"])
      assert_equal "received", app.get_event(result["event_id"])["status"]
      app.ignore(result["event_id"])

      prune = app.prune_events(statuses: ["ignored"], older_than: Time.now.to_i + 1, limit: 10)
      assert_equal 1, prune["events_pruned"]
      assert_equal "prune_events", app.list_prune_audits(limit: 10).first["kind"]

      orphan =
        app.ingest(
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "invalid-ruby",
          signature_valid: false,
          signature_error: "bad signature"
        )
      assert_nil orphan["event_id"]
      assert_equal 1, app.prune_orphan_deliveries(older_than: Time.now.to_i + 1, limit: 10)["deliveries_pruned"]
      app.close
    end
  end

  def test_ruby_rolls_back_handler_writes_and_records_retry_dead_letter
    Dir.mktmpdir("knocker-ruby-") do |dir|
      app = KnockerSqlite::Database.open(File.join(dir, "app.db"))
      app.send(:exec, "CREATE TABLE side_effects (event_id INTEGER)")
      app.add_endpoint(name: "github", path: "/webhooks/github", provider: "github", enabled: true)
      result =
        app.ingest(
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "ruby-failure-rollback",
          max_attempts: 2
        )

      app.register_handler("github") do |_event, tx|
        tx.exec("INSERT INTO side_effects (event_id) VALUES (#{result['event_id']})")
        raise "boom"
      end

      assert_raises(RuntimeError) { app.run_worker_once(worker_id: "ruby-failing-worker") }
      assert_equal 0, app.send(:scalar, "SELECT COUNT(*) FROM side_effects").to_i
      assert_equal "failed", app.get_event(result["event_id"])["status"]
      assert_equal 1, app.send(:scalar, "SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'").to_i

      assert_raises(RuntimeError) { app.run_worker_once(worker_id: "ruby-failing-worker") }
      assert_equal 0, app.send(:scalar, "SELECT COUNT(*) FROM side_effects").to_i
      assert_equal "dead", app.get_event(result["event_id"])["status"]
      assert_equal 0, app.send(:scalar, "SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'").to_i

      error_result =
        app.ingest(
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "ruby-loop-error",
          max_attempts: 1
        )
      app.register_handler("github") { |_event| raise "loop boom" }
      errors = []
      stop = false
      processed =
        app.run_worker(
          worker_id: "ruby-error-loop",
          max_jobs: 1,
          idle_poll_s: 0.001,
          should_stop: -> { stop },
          on_error: lambda { |error|
            errors << error.message
            stop = true
          }
        )
      assert_equal 0, processed
      assert_equal ["loop boom"], errors
      assert_equal "dead", app.get_event(error_result["event_id"])["status"]
      app.close
    end
  end

  def test_ruby_event_type_dispatch_worker_loop_receive_and_retention
    Dir.mktmpdir("knocker-ruby-") do |dir|
      app = KnockerSqlite::Database.open(File.join(dir, "app.db"))
      app.add_endpoint(
        name: "github",
        path: "/webhooks/github",
        provider: "github",
        enabled: true,
        secrets: ["github-secret"]
      )

      app.ingest(endpoint: "github", body: "{}", provider_delivery_id: "ruby-type-a", event_type: "push")
      app.ingest(endpoint: "github", body: "{}", provider_delivery_id: "ruby-type-b", event_type: "pull_request")
      seen = []
      app.register_handler("github") { |event| seen << "fallback:#{event['event_type']}" }
      app.register_handler("github", event_type: "pull_request") { |event| seen << "typed:#{event['event_type']}" }
      assert_equal 2, app.run_worker(worker_id: "ruby-loop-worker", max_jobs: 2, idle_poll_s: 0.001)
      assert_equal ["fallback:push", "typed:pull_request"], seen.sort

      body = "{\"zen\":\"keep it logically awesome\"}"
      received =
        app.receive(
          endpoint: "github",
          body: body,
          headers: {
            "X-Hub-Signature-256" => "sha256=2467a1987473c6ee89a22fe24f010dca30cb93d575240b9689ab697bed4b6eab",
            "X-GitHub-Delivery" => "delivery-abc",
            "X-GitHub-Event" => "push"
          }
        )
      assert_equal 204, received["status_code"]
      assert_equal "push", app.get_event(received["event_id"])["event_type"]

      app.register_handler("github") { |_event| nil }
      app.run_worker_once(worker_id: "ruby-retention-worker")
      assert_equal 1, app.run_retention(max_runs: 1, event_older_than_s: 0, now: Time.now.to_i + 10)
      audit = app.list_prune_audits(limit: 1).first
      assert_equal "prune_events", audit["kind"]
      assert_operator audit["events_pruned"], :>=, 1
      assert_equal 0, app.send(:scalar, "SELECT COUNT(*) FROM knocker_events WHERE id=#{received['event_id']}").to_i
      app.close
    end
  end
end
