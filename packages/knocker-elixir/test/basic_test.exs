defmodule KnockerSqliteTest do
  use ExUnit.Case, async: false

  test "elixir binding runs the minimal shared-contract flow" do
    db_dir = Path.join(System.tmp_dir!(), "knocker-elixir-#{System.unique_integer([:positive])}")
    File.rm_rf!(db_dir)
    File.mkdir_p!(db_dir)
    db_path = Path.join(db_dir, "app.db")

    {:ok, opened} = KnockerSqlite.open(db_path)
    parent = self()

    try do
      :ok =
        Exqlite.Sqlite3.execute(
          opened.conn,
          "CREATE TABLE handled_events (event_id INTEGER PRIMARY KEY, body TEXT)"
        )

      app =
        KnockerSqlite.register_handler(opened, "github", fn event, tx ->
          send(parent, {:handled, event["id"], event["body_blob"]})

          :ok =
            KnockerSqlite.Tx.exec(
              tx,
              "INSERT INTO handled_events (event_id, body) VALUES (#{event["id"]}, '#{event["body_blob"]}')"
            )
        end)

      {:ok, _endpoint_id} =
        KnockerSqlite.add_endpoint(app, %{
          name: "github",
          path: "/webhooks/github",
          provider: "github",
          enabled: true
        })

      {:ok, result} =
        KnockerSqlite.ingest(app, %{
          endpoint: "github",
          body: ~s({"hello":"world"}),
          provider_delivery_id: "elixir-delivery-1",
          signature_valid: true
        })

      assert is_integer(result["event_id"])

      {:ok, event} = KnockerSqlite.get_event(app, result["event_id"])
      assert event["status"] == "received"

      {:ok, events} = KnockerSqlite.list_events(app, 10)
      assert length(events) == 1

      {:ok, processed_event_id} = KnockerSqlite.run_worker_once(app, "elixir-worker")
      assert processed_event_id == result["event_id"]
      assert_receive {:handled, ^processed_event_id, ~s({"hello":"world"})}

      {:ok, handled_body} =
        KnockerSqlite.Tx.scalar(
          %KnockerSqlite.Tx{conn: opened.conn},
          "SELECT body FROM handled_events WHERE event_id=#{result["event_id"]}"
        )

      assert handled_body == ~s({"hello":"world"})

      {:ok, event} = KnockerSqlite.get_event(app, result["event_id"])
      assert event["status"] == "handled"

      {:ok, _} = KnockerSqlite.replay(app, result["event_id"])
      {:ok, event} = KnockerSqlite.get_event(app, result["event_id"])
      assert event["status"] == "received"
    after
      :ok = KnockerSqlite.close(opened)
    end
  end

  test "elixir binding exposes operator parity helpers" do
    db_dir = Path.join(System.tmp_dir!(), "knocker-elixir-#{System.unique_integer([:positive])}")
    File.rm_rf!(db_dir)
    File.mkdir_p!(db_dir)
    db_path = Path.join(db_dir, "app.db")

    {:ok, opened} = KnockerSqlite.open(db_path)

    app =
      KnockerSqlite.register_handler(opened, "github", fn _event ->
        :ok
      end)

    try do
      {:ok, _endpoint_id} =
        KnockerSqlite.add_endpoint(app, %{
          name: "github",
          path: "/webhooks/github",
          provider: "github",
          enabled: true
        })

      {:ok, result} =
        KnockerSqlite.ingest(app, %{
          endpoint: "github",
          body: ~s({"version":1}),
          provider_delivery_id: "elixir-delivery-parity",
          event_type: "v1",
          signature_valid: true
        })

      {:ok, duplicate} =
        KnockerSqlite.ingest(app, %{
          endpoint: "github",
          body: ~s({"version":2}),
          provider_delivery_id: "elixir-delivery-parity",
          event_type: "v2",
          signature_valid: true
        })

      assert duplicate["duplicate"] == 1

      {:ok, deliveries} =
        KnockerSqlite.list_deliveries(app, %{event_id: result["event_id"], limit: 10})

      assert length(deliveries) == 2
      {:ok, delivery} = KnockerSqlite.get_delivery(app, duplicate["delivery_id"])
      assert delivery["provider_delivery_id"] == "elixir-delivery-parity"

      {:ok, _} = KnockerSqlite.run_worker_once(app, "elixir-parity-worker")
      {:ok, event} = KnockerSqlite.get_event(app, result["event_id"])
      assert event["status"] == "handled"

      parent = self()

      replay_app =
        app
        |> KnockerSqlite.register_handler("github", fn _event ->
          raise "replay_delivery used endpoint fallback instead of delivery event type"
        end)
        |> KnockerSqlite.register_handler("github", "v2", fn event ->
          send(parent, {:replay_body, "#{event["event_type"]}:#{event["body_blob"]}"})
        end)

      {:ok, :replayed} = KnockerSqlite.replay_delivery(replay_app, duplicate["delivery_id"])
      {:ok, _} = KnockerSqlite.run_worker_once(replay_app, "elixir-delivery-worker")
      assert_receive {:replay_body, ~s(v2:{"version":2})}

      {:ok, _} = KnockerSqlite.replay(replay_app, result["event_id"])
      {:ok, :ignored} = KnockerSqlite.ignore(replay_app, result["event_id"])
      {:ok, _} = KnockerSqlite.requeue(replay_app, result["event_id"])
      {:ok, event} = KnockerSqlite.get_event(replay_app, result["event_id"])
      assert event["status"] == "received"
      {:ok, :ignored} = KnockerSqlite.ignore(replay_app, result["event_id"])

      {:ok, prune} =
        KnockerSqlite.prune_events(replay_app, %{
          statuses: ["ignored"],
          older_than: 9_999_999_999,
          limit: 10
        })

      assert prune["events_pruned"] == 1
      {:ok, audits} = KnockerSqlite.list_prune_audits(replay_app, 10)
      assert hd(audits)["kind"] == "prune_events"

      {:ok, orphan} =
        KnockerSqlite.ingest(replay_app, %{
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "invalid-elixir",
          signature_valid: false,
          signature_error: "bad signature"
        })

      assert orphan["event_id"] == nil

      {:ok, orphan_prune} =
        KnockerSqlite.prune_orphan_deliveries(replay_app, %{older_than: 9_999_999_999, limit: 10})

      assert orphan_prune["deliveries_pruned"] == 1
    after
      :ok = KnockerSqlite.close(opened)
    end
  end

  test "elixir binding proves failure rollback, typed dispatch, receive, worker loop, and retention" do
    db_dir = Path.join(System.tmp_dir!(), "knocker-elixir-#{System.unique_integer([:positive])}")
    File.rm_rf!(db_dir)
    File.mkdir_p!(db_dir)
    db_path = Path.join(db_dir, "app.db")

    {:ok, opened} = KnockerSqlite.open(db_path)

    try do
      :ok = Exqlite.Sqlite3.execute(opened.conn, "CREATE TABLE side_effects (event_id INTEGER)")

      {:ok, _endpoint_id} =
        KnockerSqlite.add_endpoint(opened, %{
          name: "github",
          path: "/webhooks/github",
          provider: "github",
          enabled: true
        })

      {:ok, failing} =
        KnockerSqlite.ingest(opened, %{
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "elixir-failure-rollback",
          max_attempts: 2
        })

      failing_app =
        KnockerSqlite.register_handler(opened, "github", fn _event, tx ->
          :ok =
            KnockerSqlite.Tx.exec(
              tx,
              "INSERT INTO side_effects (event_id) VALUES (#{failing["event_id"]})"
            )

          raise "boom"
        end)

      assert {:error, "boom"} = KnockerSqlite.run_worker_once(failing_app, "elixir-failing-worker")
      {:ok, side_effects} =
        KnockerSqlite.Tx.scalar(%KnockerSqlite.Tx{conn: opened.conn}, "SELECT COUNT(*) FROM side_effects")
      assert side_effects == 0
      {:ok, event} = KnockerSqlite.get_event(failing_app, failing["event_id"])
      assert event["status"] == "failed"

      assert {:error, "boom"} = KnockerSqlite.run_worker_once(failing_app, "elixir-failing-worker")
      {:ok, event} = KnockerSqlite.get_event(failing_app, failing["event_id"])
      assert event["status"] == "dead"

      {:ok, _loop_error} =
        KnockerSqlite.ingest(opened, %{
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "elixir-loop-error",
          max_attempts: 1
        })

      error_loop_app =
        KnockerSqlite.register_handler(opened, "github", fn _event ->
          raise "loop boom"
        end)

      assert {:error, "loop boom"} =
               KnockerSqlite.run_worker(error_loop_app, "elixir-error-loop", %{
                 max_jobs: 1,
                 idle_poll_ms: 1
               })

      {:ok, _} =
        KnockerSqlite.ingest(opened, %{
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "elixir-type-a",
          event_type: "push"
        })

      {:ok, _} =
        KnockerSqlite.ingest(opened, %{
          endpoint: "github",
          body: "{}",
          provider_delivery_id: "elixir-type-b",
          event_type: "pull_request"
        })

      parent = self()

      typed_app =
        opened
        |> KnockerSqlite.register_handler("github", fn event ->
          send(parent, {:dispatch, "fallback:#{event["event_type"]}"})
        end)
        |> KnockerSqlite.register_handler("github", "pull_request", fn event ->
          send(parent, {:dispatch, "typed:#{event["event_type"]}"})
        end)

      assert 2 = KnockerSqlite.run_worker(typed_app, "elixir-loop-worker", %{max_jobs: 2, idle_poll_ms: 1})
      assert_receive {:dispatch, "fallback:push"}
      assert_receive {:dispatch, "typed:pull_request"}

      body = ~s({"zen":"keep it logically awesome"})

      {:ok, received} =
        KnockerSqlite.receive(opened, %{
          endpoint: "github",
          provider: "github",
          secrets: ["github-secret"],
          body: body,
          headers: %{
            "X-Hub-Signature-256" =>
              "sha256=2467a1987473c6ee89a22fe24f010dca30cb93d575240b9689ab697bed4b6eab",
            "X-GitHub-Delivery" => "delivery-abc",
            "X-GitHub-Event" => "push"
          }
        })

      assert received["status_code"] == 204
      {:ok, received_event} = KnockerSqlite.get_event(opened, received["event_id"])
      assert received_event["event_type"] == "push"

      retention_app = KnockerSqlite.register_handler(opened, "github", fn _event -> :ok end)
      {:ok, _} = KnockerSqlite.run_worker_once(retention_app, "elixir-retention-worker")
      assert 1 =
               KnockerSqlite.run_retention(retention_app, %{
                 max_runs: 1,
                 event_older_than_s: 0,
                 now: System.system_time(:second) + 10
               })

      {:ok, audits} = KnockerSqlite.list_prune_audits(opened, 1)
      assert hd(audits)["kind"] == "prune_events"
      assert hd(audits)["events_pruned"] >= 1
      {:ok, remaining} =
        KnockerSqlite.Tx.scalar(
          %KnockerSqlite.Tx{conn: opened.conn},
          "SELECT COUNT(*) FROM knocker_events WHERE id=#{received["event_id"]}"
        )
      assert remaining == 0
    after
      :ok = KnockerSqlite.close(opened)
    end
  end
end
