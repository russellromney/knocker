defmodule KnockerSqlite do
  defstruct [:conn, handlers: %{}]

  defmodule Tx do
    defstruct [:conn]

    def exec(%__MODULE__{conn: conn}, sql) do
      Exqlite.Sqlite3.execute(conn, sql)
    end

    def scalar(%__MODULE__{conn: conn}, sql) do
      with {:ok, stmt} <- Exqlite.Sqlite3.prepare(conn, sql),
           {:ok, rows} <- Exqlite.Sqlite3.fetch_all(conn, stmt),
           :ok <- Exqlite.Sqlite3.release(conn, stmt) do
        case rows do
          [[value | _] | _] -> {:ok, value}
          _ -> {:error, :no_rows}
        end
      end
    end
  end

  def open(path) do
    with {:ok, conn} <- Exqlite.Sqlite3.open(path),
         :ok <- Exqlite.Sqlite3.enable_load_extension(conn, 1),
         :ok <- Exqlite.Sqlite3.execute(conn, "SELECT load_extension('#{extension_path()}')"),
         :ok <- Exqlite.Sqlite3.enable_load_extension(conn, 0),
         :ok <- Exqlite.Sqlite3.execute(conn, "SELECT knocker_bootstrap()") do
      {:ok, %__MODULE__{conn: conn}}
    end
  end

  def close(%__MODULE__{conn: conn}) do
    Exqlite.Sqlite3.close(conn)
  end

  def add_endpoint(%__MODULE__{conn: conn}, attrs) do
    provider = sql_literal(Map.get(attrs, :provider))
    enabled = if Map.get(attrs, :enabled, true), do: "1", else: "0"

    scalar(
      conn,
      "SELECT knocker_endpoint_upsert(#{sql_literal(attrs.name)}, #{sql_literal(attrs.path)}, #{provider}, #{enabled})"
    )
  end

  def ingest(%__MODULE__{conn: conn}, attrs) do
    method = Map.get(attrs, :method, "POST")
    headers_json = Jason.encode!(Map.get(attrs, :headers, %{}))
    query_json = Jason.encode!(Map.get(attrs, :query, %{}))
    queue_name = Map.get(attrs, :queue_name, "knocker.events")
    max_attempts = Map.get(attrs, :max_attempts, 3)

    signature_valid =
      case Map.get(attrs, :signature_valid, true) do
        nil -> "NULL"
        true -> "1"
        false -> "0"
      end

    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    sql =
      "SELECT knocker_ingest(" <>
        "#{sql_literal(attrs.endpoint)}, " <>
        "#{sql_literal(method)}, " <>
        "#{sql_literal(headers_json)}, " <>
        "#{blob_literal(Map.fetch!(attrs, :body))}, " <>
        "#{sql_literal(query_json)}, " <>
        "#{signature_valid}, " <>
        "#{sql_literal(Map.get(attrs, :signature_error))}, " <>
        "#{sql_literal(Map.get(attrs, :provider_event_id))}, " <>
        "#{sql_literal(Map.get(attrs, :provider_delivery_id))}, " <>
        "#{sql_literal(Map.get(attrs, :event_type))}, " <>
        "#{sql_literal(Map.get(attrs, :dedupe_key))}, " <>
        "#{sql_literal(queue_name)}, " <>
        "#{max_attempts})"

    case scalar(conn, sql) do
      {:ok, json} ->
        :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
        {:ok, Jason.decode!(json)}

      {:error, reason} ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        {:error, reason}
    end
  end

  def receive(%__MODULE__{conn: conn}, attrs) do
    method = Map.get(attrs, :method, "POST")
    headers_json = Jason.encode!(Map.get(attrs, :headers, %{}))
    query_json = Jason.encode!(Map.get(attrs, :query, %{}))
    secrets_json = Jason.encode!(Map.get(attrs, :secrets, []))
    provider_options_json = Jason.encode!(Map.get(attrs, :provider_options, %{}))
    queue_name = Map.get(attrs, :queue_name, "knocker.events")
    max_attempts = Map.get(attrs, :max_attempts, 3)
    provider = Map.fetch!(attrs, :provider)

    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    sql =
      "SELECT knocker_receive(" <>
        "#{sql_literal(attrs.endpoint)}, " <>
        "#{sql_literal(provider)}, " <>
        "#{sql_literal(secrets_json)}, " <>
        "#{sql_literal(provider_options_json)}, " <>
        "#{sql_literal(method)}, " <>
        "#{sql_literal(headers_json)}, " <>
        "#{blob_literal(Map.fetch!(attrs, :body))}, " <>
        "#{sql_literal(query_json)}, " <>
        "#{sql_literal(Map.get(attrs, :provider_event_id))}, " <>
        "#{sql_literal(Map.get(attrs, :provider_delivery_id))}, " <>
        "#{sql_literal(Map.get(attrs, :event_type))}, " <>
        "#{sql_literal(Map.get(attrs, :dedupe_key))}, " <>
        "#{sql_literal(queue_name)}, " <>
        "#{max_attempts})"

    case scalar(conn, sql) do
      {:ok, json} ->
        :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
        {:ok, Jason.decode!(json)}

      {:error, reason} ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        {:error, reason}
    end
  end

  def get_event(%__MODULE__{conn: conn}, event_id) do
    scalar(
      conn,
      """
      SELECT json_object(
        'id', e.id,
        'endpoint', ep.name,
        'event_type', e.event_type,
        'provider_event_id', e.provider_event_id,
        'provider_delivery_id', e.provider_delivery_id,
        'status', e.status,
        'attempt_count', e.attempt_count,
        'body_blob', CAST(e.body_blob AS TEXT)
      )
      FROM knocker_events e
      JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
      WHERE e.id=#{event_id}
      """
    )
    |> decode_json_result()
  end

  def list_events(%__MODULE__{conn: conn}, limit) do
    scalar(
      conn,
      """
      SELECT COALESCE(json_group_array(json_object(
        'id', id,
        'endpoint', endpoint,
        'event_type', event_type,
        'provider_event_id', provider_event_id,
        'provider_delivery_id', provider_delivery_id,
        'status', status,
        'attempt_count', attempt_count
      )), '[]')
      FROM (
        SELECT
          e.id AS id,
          ep.name AS endpoint,
          e.event_type AS event_type,
          e.provider_event_id AS provider_event_id,
          e.provider_delivery_id AS provider_delivery_id,
          e.status AS status,
          e.attempt_count AS attempt_count
        FROM knocker_events e
        JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
        ORDER BY e.id
        LIMIT #{limit}
      )
      """
    )
    |> decode_json_result()
  end

  def get_delivery(%__MODULE__{conn: conn}, delivery_id) do
    scalar(
      conn,
      """
      SELECT json_object(
        'id', d.id,
        'event_id', d.event_id,
        'endpoint', ep.name,
        'event_type', d.event_type,
        'provider_event_id', d.provider_event_id,
        'provider_delivery_id', d.provider_delivery_id,
        'dedupe_key', d.dedupe_key,
        'method', d.method,
        'headers_json', d.headers_json,
        'query_json', d.query_json,
        'body_blob', CAST(d.body_blob AS TEXT),
        'received_at', d.received_at,
        'signature_valid', d.signature_valid,
        'signature_error', d.signature_error
      )
      FROM knocker_deliveries d
      JOIN knocker_endpoints ep ON ep.id = d.endpoint_id
      WHERE d.id=#{delivery_id}
      """
    )
    |> decode_json_result()
  end

  def list_deliveries(%__MODULE__{conn: conn}, attrs \\ %{}) do
    clauses =
      []
      |> maybe_clause(Map.get(attrs, :event_id), fn value -> "d.event_id=#{value}" end)
      |> maybe_clause(Map.get(attrs, :endpoint), fn value -> "ep.name=#{sql_literal(value)}" end)
      |> maybe_clause(Map.get(attrs, :signature_valid), fn value ->
        if value,
          do: "d.signature_valid=1",
          else: "(d.signature_valid=0 OR d.signature_valid IS NULL)"
      end)
      |> maybe_clause(Map.get(attrs, :orphaned), fn value ->
        if value, do: "d.event_id IS NULL", else: "d.event_id IS NOT NULL"
      end)
      |> maybe_clause(Map.get(attrs, :since), fn value -> "d.received_at>=#{value}" end)

    where = if clauses == [], do: "", else: "WHERE #{Enum.join(clauses, " AND ")}"
    limit = Map.get(attrs, :limit, 100)

    scalar(
      conn,
      """
      SELECT COALESCE(json_group_array(json_object(
        'id', id,
        'event_id', event_id,
        'endpoint', endpoint,
        'event_type', event_type,
        'provider_event_id', provider_event_id,
        'provider_delivery_id', provider_delivery_id,
        'dedupe_key', dedupe_key,
        'method', method,
        'headers_json', headers_json,
        'query_json', query_json,
        'body_blob', body_blob,
        'received_at', received_at,
        'signature_valid', signature_valid,
        'signature_error', signature_error
      )), '[]')
      FROM (
        SELECT
          d.id AS id,
          d.event_id AS event_id,
          ep.name AS endpoint,
          d.event_type AS event_type,
          d.provider_event_id AS provider_event_id,
          d.provider_delivery_id AS provider_delivery_id,
          d.dedupe_key AS dedupe_key,
          d.method AS method,
          d.headers_json AS headers_json,
          d.query_json AS query_json,
          CAST(d.body_blob AS TEXT) AS body_blob,
          d.received_at AS received_at,
          d.signature_valid AS signature_valid,
          d.signature_error AS signature_error
        FROM knocker_deliveries d
        JOIN knocker_endpoints ep ON ep.id = d.endpoint_id
        #{where}
        ORDER BY d.received_at DESC, d.id DESC
        LIMIT #{limit}
      )
      """
    )
    |> decode_json_result()
  end

  def replay(%__MODULE__{conn: conn}, event_id) do
    scalar(conn, "SELECT knocker_replay(#{event_id}, 'knocker.events', 3)")
  end

  def requeue(%__MODULE__{conn: conn}, event_id) do
    scalar(conn, "SELECT knocker_requeue(#{event_id}, 'knocker.events', 3)")
  end

  def ignore(%__MODULE__{conn: conn} = app, event_id) do
    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    with {:ok, event} <- get_event(app, event_id) do
      result =
        cond do
          event["status"] == "ignored" ->
            :ok

          event["status"] in ["received", "failed", "dead"] ->
            {:ok, _} = scalar(conn, "SELECT knocker_mark_ignored(#{event_id}, 0)")
            :ok

          true ->
            _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
            {:error, "event #{event_id} with status #{event["status"]} cannot be ignored"}
        end

      case result do
        :ok ->
          :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
          {:ok, :ignored}

        other ->
          other
      end
    else
      error ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        error
    end
  end

  def replay_delivery(%__MODULE__{conn: conn} = app, delivery_id) do
    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    with {:ok, delivery} <- get_delivery(app, delivery_id),
         true <-
           not is_nil(delivery["event_id"]) ||
             {:error, "delivery #{delivery_id} is not linked to an event"},
         {:ok, event} <- get_event(app, delivery["event_id"]),
         true <-
           event["status"] in ["handled", "failed", "dead", "ignored"] ||
             {:error,
              "event #{delivery["event_id"]} with status #{event["status"]} cannot replay a delivery"},
         :ok <- delete_live_jobs_for_event(conn, delivery["event_id"]),
         {:ok, _} <- scalar(conn, "SELECT knocker_reset_event(#{delivery["event_id"]})"),
         payload <- Jason.encode!(%{event_id: delivery["event_id"], delivery_id: delivery["id"]}),
         {:ok, _} <-
           scalar(
             conn,
             "SELECT honker_enqueue('knocker.events', #{sql_literal(payload)}, NULL, NULL, 0, 3, NULL)"
           ) do
      :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
      {:ok, :replayed}
    else
      error ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        error
    end
  end

  def prune_events(%__MODULE__{conn: conn}, attrs) do
    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    result =
      scalar(
        conn,
        "SELECT knocker_prune_events(#{sql_literal(Jason.encode!(attrs.statuses))}, #{attrs.older_than}, #{attrs.limit}, 'knocker.events')"
      )
      |> decode_json_result()

    case result do
      {:ok, _} ->
        :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
        result

      other ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        other
    end
  end

  def prune_orphan_deliveries(%__MODULE__{conn: conn}, attrs) do
    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    result =
      scalar(
        conn,
        "SELECT knocker_prune_orphan_deliveries(#{attrs.older_than}, #{attrs.limit}, 'knocker.events')"
      )
      |> decode_json_result()

    case result do
      {:ok, _} ->
        :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
        result

      other ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        other
    end
  end

  def list_prune_audits(%__MODULE__{conn: conn}, limit \\ 50) do
    scalar(
      conn,
      """
      SELECT COALESCE(json_group_array(json_object(
        'id', id,
        'kind', kind,
        'queue_name', queue_name,
        'executed_at', executed_at,
        'events_pruned', events_pruned,
        'deliveries_pruned', deliveries_pruned,
        'attempts_pruned', attempts_pruned,
        'live_jobs_pruned', live_jobs_pruned,
        'summary_json', summary_json
      )), '[]')
      FROM (
        SELECT * FROM knocker_prune_audits
        ORDER BY executed_at DESC, id DESC
        LIMIT #{limit}
      )
      """
    )
    |> decode_json_result()
  end

  def run_retention_once(%__MODULE__{conn: conn}, attrs \\ %{}) do
    statuses = Map.get(attrs, :statuses, ["handled", "ignored"])
    now = Map.get(attrs, :now, System.system_time(:second))
    event_cutoff =
      case Map.get(attrs, :event_older_than_s) do
        nil -> "NULL"
        seconds -> now - seconds
      end
    orphan_cutoff =
      case Map.get(attrs, :orphan_deliveries_older_than_s) do
        nil -> "NULL"
        seconds -> now - seconds
      end
    event_limit = Map.get(attrs, :event_limit, 1000)
    orphan_limit = Map.get(attrs, :orphan_deliveries_limit, 1000)
    queue_name = Map.get(attrs, :queue_name, "knocker.events")

    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    result =
      scalar(
        conn,
        "SELECT knocker_run_retention_pass(#{sql_literal(Jason.encode!(statuses))}, #{event_cutoff}, #{event_limit}, #{orphan_cutoff}, #{orphan_limit}, #{sql_literal(queue_name)})"
      )
      |> decode_json_result()

    case result do
      {:ok, _} ->
        :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
        result

      other ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        other
    end
  end

  def run_retention(app, attrs \\ %{}) do
    max_runs = Map.get(attrs, :max_runs, 1)
    interval_ms = Map.get(attrs, :interval_ms, 60_000)
    policy = Map.drop(attrs, [:max_runs, :interval_ms])

    Enum.reduce_while(1..max_runs, 0, fn _run, count ->
      case run_retention_once(app, policy) do
        {:ok, _} ->
          if count + 1 >= max_runs do
            {:halt, count + 1}
          else
            Process.sleep(interval_ms)
            {:cont, count + 1}
          end

        {:error, reason} ->
          {:halt, {:error, reason}}
      end
    end)
  end

  def register_handler(%__MODULE__{} = app, endpoint, fun)
      when is_function(fun, 1) or is_function(fun, 2) do
    %{app | handlers: Map.put(app.handlers, handler_key(endpoint, nil), fun)}
  end

  def register_handler(%__MODULE__{} = app, endpoint, event_type, fun)
      when is_function(fun, 1) or is_function(fun, 2) do
    %{app | handlers: Map.put(app.handlers, handler_key(endpoint, event_type), fun)}
  end

  def run_worker_once(%__MODULE__{conn: conn, handlers: handlers}, worker_id) do
    with {:ok, rows_json} <-
           scalar(
             conn,
             "SELECT honker_claim_batch('knocker.events', #{sql_literal(worker_id)}, 1, 60)"
           ),
         rows <- Jason.decode!(rows_json) do
      case rows do
        [] ->
          {:ok, nil}

        [job | _] ->
          job =
            if Map.has_key?(job, "max_attempts") do
              job
            else
              {:ok, max_attempts} =
                scalar(conn, "SELECT max_attempts FROM _honker_live WHERE id=#{job["id"]}")

              Map.put(job, "max_attempts", max_attempts)
            end

          payload = Jason.decode!(job["payload"])
          event_id = payload["event_id"]
          {:ok, event} = get_event(%__MODULE__{conn: conn, handlers: handlers}, event_id)

          event =
            case Map.fetch(payload, "delivery_id") do
              {:ok, delivery_id} ->
                {:ok, delivery} =
                  get_delivery(%__MODULE__{conn: conn, handlers: handlers}, delivery_id)

                Map.merge(event, %{
                  "event_type" => delivery["event_type"],
                  "provider_event_id" => delivery["provider_event_id"],
                  "provider_delivery_id" => delivery["provider_delivery_id"],
                  "dedupe_key" => delivery["dedupe_key"],
                  "body_blob" => delivery["body_blob"]
                })

              :error ->
                event
            end

          handler =
            Map.get(handlers, handler_key(event["endpoint"], event["event_type"])) ||
              Map.get(handlers, handler_key(event["endpoint"], nil))

          case handler do
            nil ->
              message = "no handler registered for endpoint #{inspect(event["endpoint"])}"
              :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

              {:ok, _} =
                scalar(
                  conn,
                  "SELECT knocker_mark_failed(#{event_id}, #{job["attempts"]}, #{sql_literal(message)}, 1, 0)"
                )

              {:ok, _} =
                scalar(
                  conn,
                  "SELECT honker_fail(#{job["id"]}, #{sql_literal(worker_id)}, #{sql_literal(message)})"
                )

              :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
              {:error, message}

            handler ->
              :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

              try do
                {:ok, _} =
                  scalar(conn, "SELECT knocker_mark_processing(#{event_id}, #{job["attempts"]})")

                invoke_handler(handler, event, %Tx{conn: conn})

                {:ok, _} = scalar(conn, "SELECT knocker_mark_handled(#{event_id}, 0)")

                {:ok, _} =
                  scalar(conn, "SELECT honker_ack(#{job["id"]}, #{sql_literal(worker_id)})")

                :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
                {:ok, event_id}
              rescue
                error ->
                  message = Exception.message(error)
                  _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
                  :ok = record_failed_claim(conn, worker_id, job, event_id, message)
                  {:error, message}
              end
          end
      end
    end
  end

  def run_worker(%__MODULE__{} = app, worker_id, attrs \\ %{}) do
    max_jobs = Map.get(attrs, :max_jobs, 1)
    idle_poll_ms = Map.get(attrs, :idle_poll_ms, 100)

    Enum.reduce_while(1..max_jobs, 0, fn _job, count ->
      case run_worker_once(app, worker_id) do
        {:ok, nil} ->
          Process.sleep(idle_poll_ms)
          {:cont, count}

        {:ok, _event_id} ->
          {:cont, count + 1}

        {:error, reason} ->
          {:halt, {:error, reason}}
      end
    end)
  end

  defp decode_json_result({:ok, json}), do: {:ok, Jason.decode!(json)}
  defp decode_json_result(other), do: other

  defp maybe_clause(clauses, nil, _fun), do: clauses
  defp maybe_clause(clauses, value, fun), do: clauses ++ [fun.(value)]

  defp handler_key(endpoint, nil), do: "#{endpoint}\0"
  defp handler_key(endpoint, event_type), do: "#{endpoint}\0#{event_type}"

  defp record_failed_claim(conn, worker_id, job, event_id, message) do
    attempt_count = job["attempts"]
    terminal = attempt_count >= job["max_attempts"]
    :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

    mark =
      scalar(
        conn,
        "SELECT knocker_mark_failed(#{event_id}, #{attempt_count}, #{sql_literal(message)}, #{if terminal, do: 1, else: 0}, 0)"
      )

    queue_result =
      if terminal do
        scalar(
          conn,
          "SELECT honker_fail(#{job["id"]}, #{sql_literal(worker_id)}, #{sql_literal(message)})"
        )
      else
        scalar(
          conn,
          "SELECT honker_retry(#{job["id"]}, #{sql_literal(worker_id)}, 0, #{sql_literal(message)})"
        )
      end

    case {mark, queue_result} do
      {{:ok, _}, {:ok, _}} ->
        :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")

      _ ->
        _ = Exqlite.Sqlite3.execute(conn, "ROLLBACK")
        {:error, :failed_to_record_claim_failure}
    end
  end

  defp invoke_handler(handler, event, tx) do
    case :erlang.fun_info(handler, :arity) do
      {:arity, 2} -> handler.(event, tx)
      _ -> handler.(event)
    end
  end

  defp delete_live_jobs_for_event(conn, event_id) do
    with {:ok, rows_json} <-
           scalar(
             conn,
             "SELECT COALESCE(json_group_array(json_object('id', id, 'payload', payload)), '[]') FROM _honker_live WHERE queue='knocker.events'"
           ),
         rows <- Jason.decode!(rows_json) do
      Enum.each(rows, fn row ->
        case Jason.decode(row["payload"]) do
          {:ok, %{"event_id" => ^event_id}} ->
            :ok = Exqlite.Sqlite3.execute(conn, "DELETE FROM _honker_live WHERE id=#{row["id"]}")

          _ ->
            :ok
        end
      end)

      :ok
    end
  end

  defp scalar(conn, sql) do
    with {:ok, stmt} <- Exqlite.Sqlite3.prepare(conn, sql),
         {:ok, rows} <- Exqlite.Sqlite3.fetch_all(conn, stmt),
         :ok <- Exqlite.Sqlite3.release(conn, stmt) do
      case rows do
        [[value | _] | _] -> {:ok, value}
        _ -> {:error, :no_rows}
      end
    end
  end

  defp sql_literal(nil), do: "NULL"
  defp sql_literal(value) when is_binary(value), do: "'#{String.replace(value, "'", "''")}'"
  defp sql_literal(value), do: sql_literal(to_string(value))

  defp blob_literal(value) when is_binary(value) do
    "X'#{Base.encode16(value, case: :lower)}'"
  end

  defp extension_path do
    ext =
      case :os.type() do
        {:unix, :darwin} -> "dylib"
        {:win32, _} -> "dll"
        _ -> "so"
      end

    Path.expand("../../../target/release/libknocker_ext.#{ext}", __DIR__)
  end
end
