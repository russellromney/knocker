use rusqlite::Connection;
use rusqlite::ffi;
use std::os::raw::{c_char, c_int};

#[unsafe(no_mangle)]
pub unsafe extern "C" fn sqlite3_knockerext_init(
    db: *mut ffi::sqlite3,
    pz_err_msg: *mut *mut c_char,
    p_api: *mut ffi::sqlite3_api_routines,
) -> c_int {
    unsafe {
        Connection::extension_init2(db, pz_err_msg, p_api, |conn| {
            honker_core::attach_notify(&conn).map_err(|e| {
                rusqlite::Error::UserFunctionError(Box::new(std::io::Error::other(e.to_string())))
            })?;
            honker_core::attach_honker_functions(&conn)?;
            knocker_core::attach_knocker_functions(&conn)?;
            knocker_core::bootstrap_knocker_schema(&conn).map_err(|e| {
                rusqlite::Error::UserFunctionError(Box::new(std::io::Error::other(e.to_string())))
            })?;
            Ok(true)
        })
    }
}
