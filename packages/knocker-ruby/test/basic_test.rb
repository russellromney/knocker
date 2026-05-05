require "minitest/autorun"
require "tmpdir"
require_relative "../lib/knocker_sqlite"

class KnockerSqliteTest < Minitest::Test
  def test_ruby_binding_end_to_end
    Dir.mktmpdir("knocker-ruby-") do |dir|
      db_path = File.join(dir, "app.db")
      app = KnockerSqlite::Database.open(db_path)

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
      app.register_handler("github") do |event|
        handled << event["id"]
        assert_equal "{\"hello\":\"world\"}", event["body_blob"]
      end

      processed_event_id = app.run_worker_once(worker_id: "ruby-worker")
      assert_equal result["event_id"], processed_event_id
      assert_equal [result["event_id"]], handled

      event = app.get_event(result["event_id"])
      assert_equal "handled", event["status"]

      app.replay(result["event_id"])
      event = app.get_event(result["event_id"])
      assert_equal "received", event["status"]

      app.close
    end
  end
end
