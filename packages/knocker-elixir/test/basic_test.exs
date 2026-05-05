defmodule KnockerSqliteTest do
  use ExUnit.Case, async: false

  test "elixir binding runs the minimal shared-contract flow" do
    db_dir = Path.join(System.tmp_dir!(), "knocker-elixir-#{System.unique_integer([:positive])}")
    File.mkdir_p!(db_dir)
    db_path = Path.join(db_dir, "app.db")

    {:ok, opened} = KnockerSqlite.open(db_path)
    parent = self()

    app =
      KnockerSqlite.register_handler(opened, "github", fn event ->
        send(parent, {:handled, event["id"], event["body_blob"]})
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

      {:ok, event} = KnockerSqlite.get_event(app, result["event_id"])
      assert event["status"] == "handled"

      {:ok, _} = KnockerSqlite.replay(app, result["event_id"])
      {:ok, event} = KnockerSqlite.get_event(app, result["event_id"])
      assert event["status"] == "received"
    after
      :ok = KnockerSqlite.close(app)
    end
  end
end
