defmodule KnockerSqlite.MixProject do
  use Mix.Project

  def project do
    [
      app: :knocker_sqlite,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      deps: deps()
    ]
  end

  def application do
    [extra_applications: [:logger]]
  end

  defp deps do
    [
      {:exqlite, "~> 0.36"},
      {:jason, "~> 1.4"}
    ]
  end
end
