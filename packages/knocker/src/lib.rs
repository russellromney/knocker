use honker_core::{Readers, SharedUpdateWatcher, Writer, open_conn};
use knocker_core::attach_knocker_functions;
use parking_lot::Mutex;
use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyBytes, PyDict, PyList};
use rusqlite::Connection;
use rusqlite::types::{Value, ValueRef};
use std::sync::Arc;

fn core_err<E: std::fmt::Display>(e: E) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

fn py_to_value(item: &Bound<'_, PyAny>) -> PyResult<Value> {
    if item.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(b) = item.cast::<PyBool>() {
        return Ok(Value::Integer(if b.is_true() { 1 } else { 0 }));
    }
    if let Ok(b) = item.cast::<PyBytes>() {
        return Ok(Value::Blob(b.as_bytes().to_vec()));
    }
    if let Ok(i) = item.extract::<i64>() {
        return Ok(Value::Integer(i));
    }
    if let Ok(f) = item.extract::<f64>() {
        return Ok(Value::Real(f));
    }
    if let Ok(s) = item.extract::<String>() {
        return Ok(Value::Text(s));
    }
    let tname = item
        .get_type()
        .name()
        .map(|s| s.to_string())
        .unwrap_or_else(|_| "<unknown>".to_string());
    Err(PyTypeError::new_err(format!(
        "unsupported SQL parameter type: {}",
        tname
    )))
}

fn build_params(params: Option<&Bound<'_, PyList>>) -> PyResult<Vec<Value>> {
    let mut out = Vec::new();
    if let Some(p) = params {
        for item in p.iter() {
            out.push(py_to_value(&item)?);
        }
    }
    Ok(out)
}

fn run_query<'py>(
    py: Python<'py>,
    conn: &Connection,
    sql: &str,
    params: Option<&Bound<'_, PyList>>,
) -> PyResult<Bound<'py, PyList>> {
    let values = build_params(params)?;
    let mut stmt = conn.prepare_cached(sql).map_err(core_err)?;
    let columns: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
    let mut rows = stmt
        .query(rusqlite::params_from_iter(values))
        .map_err(core_err)?;
    let out = PyList::empty(py);
    while let Some(row) = rows.next().map_err(core_err)? {
        let dict = PyDict::new(py);
        for (i, name) in columns.iter().enumerate() {
            let v = row.get_ref(i).map_err(core_err)?;
            match v {
                ValueRef::Null => dict.set_item(name, py.None())?,
                ValueRef::Integer(iv) => dict.set_item(name, iv)?,
                ValueRef::Real(fv) => dict.set_item(name, fv)?,
                ValueRef::Text(t) => {
                    let s = std::str::from_utf8(t).unwrap_or("");
                    dict.set_item(name, s)?
                }
                ValueRef::Blob(b) => dict.set_item(name, b)?,
            }
        }
        out.append(dict)?;
    }
    Ok(out)
}

fn run_execute(
    conn: &Connection,
    sql: &str,
    params: Option<&Bound<'_, PyList>>,
) -> PyResult<usize> {
    let values = build_params(params)?;
    let mut stmt = conn.prepare_cached(sql).map_err(core_err)?;
    stmt.execute(rusqlite::params_from_iter(values)).map_err(core_err)
}

fn run_cached_noparams(conn: &Connection, sql: &str) -> rusqlite::Result<()> {
    let mut stmt = conn.prepare_cached(sql)?;
    stmt.execute([])?;
    Ok(())
}

#[pyclass]
struct Database {
    writer: Arc<Writer>,
    readers: Arc<Readers>,
    db_path: std::path::PathBuf,
    shared_watcher: Mutex<Option<Arc<SharedUpdateWatcher>>>,
}

#[pymethods]
impl Database {
    #[new]
    #[pyo3(signature = (path, max_readers=8))]
    fn new(path: String, max_readers: usize) -> PyResult<Self> {
        let writer_conn = open_conn(&path, true).map_err(core_err)?;
        honker_core::attach_honker_functions(&writer_conn).map_err(core_err)?;
        attach_knocker_functions(&writer_conn).map_err(core_err)?;
        knocker_core::bootstrap_knocker_schema(&writer_conn).map_err(core_err)?;

        Ok(Self {
            writer: Arc::new(Writer::new(writer_conn)),
            readers: Arc::new(Readers::new(path.clone(), max_readers)),
            db_path: path.into(),
            shared_watcher: Mutex::new(None),
        })
    }

    fn transaction(&self) -> PyResult<Transaction> {
        Ok(Transaction {
            writer: self.writer.clone(),
            inner: Arc::new(Mutex::new(TxState::default())),
        })
    }

    fn wal_events(&self) -> PyResult<WalEvents> {
        let shared = {
            let mut guard = self.shared_watcher.lock();
            if let Some(existing) = guard.as_ref() {
                existing.clone()
            } else {
                let watcher = Arc::new(SharedUpdateWatcher::new(self.db_path.clone()));
                *guard = Some(watcher.clone());
                watcher
            }
        };
        let (sub_id, rx) = shared.subscribe();
        Ok(WalEvents {
            db_path: self.db_path.clone(),
            shared,
            sub_id,
            inner: Arc::new(Mutex::new(WalWatchState {
                rx: Some(rx),
                queue: None,
            })),
        })
    }

    fn update_events(&self) -> PyResult<WalEvents> {
        self.wal_events()
    }

    #[pyo3(signature = (sql, params=None))]
    fn query<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: Option<Bound<'py, PyList>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let conn = self.readers.acquire().map_err(core_err)?;
        let result = run_query(py, &conn, &sql, params.as_ref());
        self.readers.release(conn);
        result
    }

    fn close(&self) {
        let watcher = {
            let mut guard = self.shared_watcher.lock();
            guard.take()
        };
        if let Some(shared) = watcher {
            let _ = shared.close();
        }
        self.writer.close();
        self.readers.close();
    }
}

#[derive(Default)]
struct TxState {
    conn: Option<Connection>,
    started: bool,
    released: bool,
}

#[pyclass]
struct Transaction {
    writer: Arc<Writer>,
    inner: Arc<Mutex<TxState>>,
}

impl Drop for Transaction {
    fn drop(&mut self) {
        let mut state = self.inner.lock();
        if !state.released {
            if let Some(conn) = state.conn.take() {
                if state.started {
                    let _ = run_cached_noparams(&conn, "ROLLBACK");
                }
                self.writer.release(conn);
            }
            state.released = true;
        }
    }
}

#[pymethods]
impl Transaction {
    fn __enter__<'a>(slf: PyRef<'a, Self>, py: Python<'a>) -> PyResult<PyRef<'a, Self>> {
        let writer = slf.writer.clone();
        let conn = match writer.try_acquire() {
            Some(c) => c,
            None => py
                .detach(|| writer.acquire())
                .ok_or_else(|| PyRuntimeError::new_err("writer is closed"))?,
        };
        match run_cached_noparams(&conn, "BEGIN IMMEDIATE") {
            Ok(()) => {
                {
                    let mut state = slf.inner.lock();
                    state.conn = Some(conn);
                    state.started = true;
                    state.released = false;
                }
                Ok(slf)
            }
            Err(e) => {
                slf.writer.release(conn);
                Err(core_err(e))
            }
        }
    }

    fn __exit__(
        &self,
        _py: Python<'_>,
        exc_type: Option<&Bound<'_, PyAny>>,
        _exc_value: Option<&Bound<'_, PyAny>>,
        _tb: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let mut state = self.inner.lock();
        if state.released || state.conn.is_none() {
            return Ok(false);
        }
        let conn = state.conn.take().unwrap();
        let raised = exc_type.map_or(false, |e| !e.is_none());
        let was_started = state.started;
        state.started = false;
        let err = if was_started {
            if raised {
                run_cached_noparams(&conn, "ROLLBACK").err()
            } else {
                match run_cached_noparams(&conn, "COMMIT") {
                    Ok(()) => None,
                    Err(e) => {
                        let _ = run_cached_noparams(&conn, "ROLLBACK");
                        Some(e)
                    }
                }
            }
        } else {
            None
        };
        self.writer.release(conn);
        state.released = true;
        if let Some(e) = err {
            return Err(core_err(e));
        }
        Ok(false)
    }

    #[pyo3(signature = (sql, params=None))]
    fn execute(
        &self,
        _py: Python<'_>,
        sql: String,
        params: Option<Bound<'_, PyList>>,
    ) -> PyResult<()> {
        let state = self.inner.lock();
        let conn = state
            .conn
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Transaction not started"))?;
        run_execute(conn, &sql, params.as_ref())?;
        Ok(())
    }

    #[pyo3(signature = (sql, params=None))]
    fn query<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: Option<Bound<'py, PyList>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let state = self.inner.lock();
        let conn = state
            .conn
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Transaction not started"))?;
        run_query(py, conn, &sql, params.as_ref())
    }

    fn bootstrap_honker_schema(&self) -> PyResult<()> {
        let state = self.inner.lock();
        let conn = state
            .conn
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Transaction not started"))?;
        honker_core::bootstrap_honker_schema(conn).map_err(core_err)
    }
}

struct WalWatchState {
    rx: Option<std::sync::mpsc::Receiver<()>>,
    queue: Option<Py<PyAny>>,
}

#[pyclass]
struct WalEvents {
    db_path: std::path::PathBuf,
    shared: Arc<SharedUpdateWatcher>,
    sub_id: u64,
    inner: Arc<Mutex<WalWatchState>>,
}

impl Drop for WalEvents {
    fn drop(&mut self) {
        self.shared.unsubscribe(self.sub_id);
    }
}

impl WalEvents {
    fn ensure_started(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let mut state = self.inner.lock();
        if let Some(queue) = &state.queue {
            return Ok(queue.clone_ref(py));
        }
        let asyncio = py.import("asyncio")?;
        let queue = asyncio.call_method0("Queue")?;
        let loop_obj = asyncio.call_method0("get_running_loop")?;

        let queue_py: Py<PyAny> = queue.clone().unbind();
        let queue_py_for_thread = queue_py.clone_ref(py);
        let loop_py: Py<PyAny> = loop_obj.unbind();
        let rx = state.rx.take().expect("wal rx already taken");

        std::thread::Builder::new()
            .name("knocker-wal-bridge".into())
            .spawn(move || {
                while rx.recv().is_ok() {
                    Python::attach(|py| {
                        let put = match queue_py_for_thread.getattr(py, "put_nowait") {
                            Ok(v) => v,
                            Err(_) => return,
                        };
                        let _ = loop_py.call_method1(py, "call_soon_threadsafe", (put, py.None()));
                    });
                }
            })
            .map_err(core_err)?;

        state.queue = Some(queue_py.clone_ref(py));
        Ok(queue_py)
    }
}

#[pymethods]
impl WalEvents {
    fn __aiter__<'a>(slf: PyRef<'a, Self>, py: Python<'a>) -> PyResult<PyRef<'a, Self>> {
        slf.ensure_started(py)?;
        Ok(slf)
    }

    fn __anext__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let queue = self.ensure_started(py)?;
        queue.bind(py).call_method0("get")
    }

    #[getter]
    fn path(&self) -> String {
        self.db_path.to_string_lossy().into_owned()
    }

    fn close(&self) {}
}

#[pyfunction]
#[pyo3(signature = (path, max_readers=8))]
fn open(path: String, max_readers: usize) -> PyResult<Database> {
    Database::new(path, max_readers)
}

#[pymodule]
fn _knocker_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(open, m)?)?;
    m.add_class::<Database>()?;
    m.add_class::<Transaction>()?;
    m.add_class::<WalEvents>()?;
    Ok(())
}
