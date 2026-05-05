defmodule KnockerSqlite do
  defstruct [:conn, handlers: %{}]

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

  def replay(%__MODULE__{conn: conn}, event_id) do
    scalar(conn, "SELECT knocker_replay(#{event_id}, 'knocker.events', 3)")
  end

  def register_handler(%__MODULE__{} = app, endpoint, fun) when is_function(fun, 1) do
    %{app | handlers: Map.put(app.handlers, to_string(endpoint), fun)}
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
          payload = Jason.decode!(job["payload"])
          event_id = payload["event_id"]
          {:ok, event} = get_event(%__MODULE__{conn: conn, handlers: handlers}, event_id)

          case Map.fetch(handlers, event["endpoint"]) do
            :error ->
              message = "no handler registered for endpoint #{inspect(event["endpoint"])}"
              :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")
              {:ok, _} =
                scalar(
                  conn,
                  "SELECT knocker_mark_failed(#{event_id}, #{job["attempts"]}, #{sql_literal(message)}, 1, 0)"
                )
              {:ok, _} =
                scalar(conn, "SELECT honker_fail(#{job["id"]}, #{sql_literal(worker_id)}, #{sql_literal(message)})")
              :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
              {:error, message}

            {:ok, handler} ->
              :ok = Exqlite.Sqlite3.execute(conn, "BEGIN IMMEDIATE")

              try do
                {:ok, _} =
                  scalar(conn, "SELECT knocker_mark_processing(#{event_id}, #{job["attempts"]})")

                handler.(event)

                {:ok, _} = scalar(conn, "SELECT knocker_mark_handled(#{event_id}, 0)")
                {:ok, _} =
                  scalar(conn, "SELECT honker_ack(#{job["id"]}, #{sql_literal(worker_id)})")
                :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
                {:ok, event_id}
              rescue
                error ->
                  message = Exception.message(error)
                  terminal = job["attempts"] >= job["max_attempts"]
                  {:ok, _} =
                    scalar(
                      conn,
                      "SELECT knocker_mark_failed(#{event_id}, #{job["attempts"]}, #{sql_literal(message)}, #{if terminal, do: 1, else: 0}, 0)"
                    )

                  {:ok, _} =
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

                  :ok = Exqlite.Sqlite3.execute(conn, "COMMIT")
                  {:error, message}
              end
          end
      end
  end
  end

  defp decode_json_result({:ok, json}), do: {:ok, Jason.decode!(json)}
  defp decode_json_result(other), do: other

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
