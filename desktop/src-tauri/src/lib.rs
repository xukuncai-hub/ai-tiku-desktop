use chrono::{Local, TimeZone};
use rusqlite::{params, Connection};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::fs::{self, OpenOptions};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, WindowEvent};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Serialize)]
struct BalanceInfo {
    supported: bool,
    is_available: bool,
    currency: String,
    total_balance: String,
    granted_balance: String,
    topped_up_balance: String,
    message: String,
}

#[derive(Serialize, Default)]
struct CostStats {
    today_cost_rmb: f64,
    total_cost_rmb: f64,
    today_tokens: i64,
    total_prompt_tokens: i64,
    total_completion_tokens: i64,
    total_calls: i64,
}

#[derive(Serialize)]
struct PathBreakdown {
    local: i64,
    cache: i64,
    bank_ai: i64,
    ai: i64,
    error: i64,
}

#[derive(Serialize)]
struct ServiceStatus {
    project_root: String,
    ai_running: bool,
    tiku_running: bool,
    local_bank_count: i64,
    answers_count: i64,
    history_count: i64,
    error_count: i64,
    breakdown: PathBreakdown,
    cost: CostStats,
}

#[derive(Serialize)]
struct HistoryItem {
    id: i64,
    time_str: String,
    title: String,
    options: String,
    q_type: String,
    answer: String,
    analysis: String,
    path: String,
}

#[derive(Serialize)]
struct LocalBankItem {
    key: String,
    title: String,
    options: String,
    q_type: String,
    answer: String,
    answer_text: String,
    analysis: String,
    updated_str: String,
}

fn hide_window_if_windows(cmd: &mut Command) {
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    #[cfg(not(windows))]
    let _ = cmd;
}

fn find_project_root() -> PathBuf {
    if let Ok(env_root) = std::env::var("AI_TIKU_ROOT") {
        let p = PathBuf::from(env_root);
        if p.join("ai-server").join("main.py").exists() {
            return p;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors() {
            if ancestor.join("ai-server").join("main.py").exists() {
                return ancestor.to_path_buf();
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        for ancestor in cwd.ancestors() {
            if ancestor.join("ai-server").join("main.py").exists() {
                return ancestor.to_path_buf();
            }
        }
    }
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".to_string());
    let home_path = PathBuf::from(&home);
    for candidate in [
        home_path.join("桌面").join("AI刷题"),
        home_path.join("Desktop").join("AI刷题"),
        PathBuf::from("/opt/ai-tiku"),
    ] {
        if candidate.join("ai-server").join("main.py").exists() {
            return candidate;
        }
    }
    home_path.join("桌面").join("AI刷题")
}

fn is_port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(120)).is_ok()
}

fn db_path(root: &Path) -> PathBuf {
    root.join("ai-server").join(".runtime").join("answers.sqlite3")
}

fn log_file_path(target: &str) -> PathBuf {
    let name = if target == "tiku" {
        "tikuadapter.log"
    } else {
        "ocsllm.log"
    };
    std::env::temp_dir().join(name)
}

fn pid_file_path(name: &str) -> PathBuf {
    std::env::temp_dir().join(format!("ai-tiku-{name}.pid"))
}

fn save_pid(name: &str, pid: u32) {
    let _ = fs::write(pid_file_path(name), pid.to_string());
}

fn kill_saved_pid(name: &str) {
    let p = pid_file_path(name);
    if let Ok(content) = fs::read_to_string(&p) {
        if let Ok(pid) = content.trim().parse::<u32>() {
            #[cfg(windows)]
            {
                let mut cmd = Command::new("taskkill");
                cmd.args(["/F", "/T", "/PID", &pid.to_string()])
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());
                hide_window_if_windows(&mut cmd);
                let _ = cmd.status();
            }
            #[cfg(unix)]
            {
                let _ = Command::new("kill")
                    .args(["-TERM", &pid.to_string()])
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
        }
        let _ = fs::remove_file(&p);
    }
}

fn norm_text(s: &str) -> String {
    const PUNCT: &str = "，,。.、；;：:'\"“”‘’()（）[]【】";
    s.chars()
        .filter(|c| !c.is_whitespace() && !PUNCT.contains(*c))
        .flat_map(|c| c.to_lowercase())
        .collect()
}

fn sha256_hex(input: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(input.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn strip_ansi(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for ch in chars.by_ref() {
                if ('@'..='~').contains(&ch) {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

fn enhanced_path() -> String {
    let cur = std::env::var("PATH").unwrap_or_default();
    if let Ok(home) = std::env::var("HOME") {
        let local_bin = format!("{home}/.local/bin");
        let sep = if cfg!(windows) { ";" } else { ":" };
        return format!("{local_bin}{sep}{cur}");
    }
    cur
}

fn get_status_sync() -> ServiceStatus {
    let root = find_project_root();
    let ai_running = is_port_open(5000);
    let tiku_running = is_port_open(8060);

    let mut local_bank_count = 0;
    let mut answers_count = 0;
    let mut history_count = 0;
    let mut error_count = 0;
    let mut breakdown = PathBreakdown {
        local: 0,
        cache: 0,
        bank_ai: 0,
        ai: 0,
        error: 0,
    };
    let mut cost = CostStats::default();

    let db = db_path(&root);
    if db.exists() {
        if let Ok(conn) = Connection::open(&db) {
            let _ = conn.busy_timeout(Duration::from_millis(500));
            local_bank_count = conn
                .query_row("SELECT COUNT(*) FROM local_bank", [], |r| r.get(0))
                .unwrap_or(0);
            answers_count = conn
                .query_row("SELECT COUNT(*) FROM answers", [], |r| r.get(0))
                .unwrap_or(0);
            history_count = conn
                .query_row("SELECT COUNT(*) FROM history", [], |r| r.get(0))
                .unwrap_or(0);
            if let Ok(mut stmt) = conn.prepare("SELECT path, COUNT(*) FROM history GROUP BY path") {
                if let Ok(rows) = stmt.query_map([], |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                        r.get::<_, i64>(1)?,
                    ))
                }) {
                    for row in rows.flatten() {
                        match row.0.as_str() {
                            "local" => breakdown.local += row.1,
                            "cache" => breakdown.cache += row.1,
                            "bank" | "bank+ai" => breakdown.bank_ai += row.1,
                            "ai" => breakdown.ai += row.1,
                            "error" => {
                                breakdown.error += row.1;
                                error_count += row.1;
                            }
                            _ => {}
                        }
                    }
                }
            }
            if let Ok((calls, p_tok, c_tok, total_rmb)) = conn.query_row(
                "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cost_rmb),0.0) FROM usage_logs",
                [],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, i64>(2)?, r.get::<_, f64>(3)?)),
            ) {
                cost.total_calls = calls;
                cost.total_prompt_tokens = p_tok;
                cost.total_completion_tokens = c_tok;
                cost.total_cost_rmb = total_rmb;
            }
            let today_start = Local::now()
                .date_naive()
                .and_hms_opt(0, 0, 0)
                .and_then(|dt| Local.from_local_datetime(&dt).single())
                .map(|dt| dt.timestamp() as f64)
                .unwrap_or(0.0);
            if let Ok((t_tok, t_rmb)) = conn.query_row(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens),0), COALESCE(SUM(cost_rmb),0.0) FROM usage_logs WHERE ts >= ?1",
                params![today_start],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, f64>(1)?)),
            ) {
                cost.today_tokens = t_tok;
                cost.today_cost_rmb = t_rmb;
            }
        }
    }

    ServiceStatus {
        project_root: root.display().to_string(),
        ai_running,
        tiku_running,
        local_bank_count,
        answers_count,
        history_count,
        error_count,
        breakdown,
        cost,
    }
}

#[tauri::command]
async fn get_status() -> Result<ServiceStatus, String> {
    tauri::async_runtime::spawn_blocking(get_status_sync)
        .await
        .map_err(|e| format!("后台任务失败: {e}"))
}

fn start_services_sync() -> Result<String, String> {
    let root = find_project_root();

    // 1) 启动 tikuAdapter (8060)
    if !is_port_open(8060) {
        let tiku_dir = root.join("tiku-adapter");
        let bin_name = if cfg!(windows) { "tikuAdapter.exe" } else { "tikuAdapter" };
        let tiku_bin = tiku_dir.join(bin_name);
        if tiku_bin.exists() {
            let log_file = OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_file_path("tiku"))
                .ok();
            let mut cmd = Command::new(&tiku_bin);
            cmd.current_dir(&tiku_dir).stdin(Stdio::null());
            if let Some(f) = log_file {
                if let Ok(f2) = f.try_clone() {
                    cmd.stdout(Stdio::from(f)).stderr(Stdio::from(f2));
                }
            }
            hide_window_if_windows(&mut cmd);
            if let Ok(child) = cmd.spawn() {
                save_pid("tiku", child.id());
            }
        }
    }

    // 2) 启动 ai-server (5000)
    if is_port_open(5000) {
        return Ok("服务已经在运行".to_string());
    }

    let ai_dir = root.join("ai-server");
    if !ai_dir.join("main.py").exists() {
        return Err(format!("找不到 ai-server/main.py（当前目录: {}）", root.display()));
    }

    let log_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(log_file_path("ai"))
        .map_err(|e| format!("无法创建日志文件: {e}"))?;
    let log_err = log_file
        .try_clone()
        .map_err(|e| format!("克隆日志句柄失败: {e}"))?;

    let mut cmd = Command::new("uv");
    cmd.current_dir(&ai_dir)
        .env("PATH", enhanced_path())
        .args(["run", "--locked", "python", "main.py"])
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(log_err));
    hide_window_if_windows(&mut cmd);

    let child = cmd.spawn().map_err(|e| format!("启动 AI 服务失败（请确认已安装 uv）: {e}"))?;
    save_pid("ai", child.id());

    for _ in 0..30 {
        std::thread::sleep(Duration::from_millis(500));
        if is_port_open(5000) {
            return Ok("已启动 AI 服务 (:5000) 与题库适配器 (:8060)".to_string());
        }
    }
    Err("启动超时，请查看 AI 服务日志".to_string())
}

fn stop_services_sync() -> Result<String, String> {
    kill_saved_pid("ai");
    kill_saved_pid("tiku");

    #[cfg(unix)]
    {
        let root = find_project_root();
        let stop_sh = root.join("停止.sh");
        if stop_sh.exists() {
            let _ = Command::new("bash")
                .arg(&stop_sh)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
    }

    #[cfg(windows)]
    {
        for name in ["tikuAdapter.exe", "python.exe"] {
            let mut cmd = Command::new("taskkill");
            cmd.args(["/F", "/IM", name])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            hide_window_if_windows(&mut cmd);
            let _ = cmd.status();
        }
    }

    Ok("已停止服务".to_string())
}

#[tauri::command]
async fn start_services() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(start_services_sync)
        .await
        .map_err(|e| format!("后台任务失败: {e}"))?
}

#[tauri::command]
async fn stop_services() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(stop_services_sync)
        .await
        .map_err(|e| format!("后台任务失败: {e}"))?
}

#[tauri::command]
async fn read_logs(target: String, lines: usize) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let path = log_file_path(&target);
        let content = match fs::read_to_string(path) {
            Ok(c) => strip_ansi(&c),
            Err(_) => return Ok(String::new()),
        };
        let all: Vec<&str> = content.lines().collect();
        let start = all.len().saturating_sub(lines);
        Ok(all[start..].join("\n"))
    })
    .await
    .map_err(|e| format!("读取日志失败: {e}"))?
}

#[tauri::command]
async fn clear_logs(target: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        fs::write(log_file_path(&target), "").map_err(|e| format!("清空日志失败: {e}"))?;
        Ok("日志已清空".to_string())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn get_ocs_config() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let root = find_project_root();
        fs::read_to_string(root.join("ai-server").join("ocs_config.json"))
            .map_err(|e| format!("读取 ocs_config.json 失败: {e}"))
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
fn open_external(target: String) -> Result<(), String> {
    let dest = match target.as_str() {
        "project" => find_project_root().display().to_string(),
        "tiku_admin" => "http://127.0.0.1:8060".to_string(),
        "recent_api" => "http://127.0.0.1:5000/recent".to_string(),
        _ => target,
    };

    #[cfg(windows)]
    {
        let mut cmd = Command::new("cmd");
        cmd.args(["/C", "start", "", &dest])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hide_window_if_windows(&mut cmd);
        cmd.spawn().map_err(|e| format!("无法打开 {dest}: {e}"))?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(&dest)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("无法打开 {dest}: {e}"))?;
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(&dest)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("无法打开 {dest}: {e}"))?;
    }

    Ok(())
}

#[tauri::command]
async fn read_env_config() -> Result<HashMap<String, String>, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let root = find_project_root();
        let env_path = root.join("ai-server").join(".env");
        let content = fs::read_to_string(&env_path).unwrap_or_default();
        let mut map = HashMap::new();
        for line in content.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }
            if let Some((k, v)) = trimmed.split_once('=') {
                let key = k.trim().to_string();
                let val = v.trim().trim_matches('"').trim_matches('\'').to_string();
                map.insert(key, val);
            }
        }
        Ok(map)
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn save_env_config(entries: BTreeMap<String, String>) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        let env_path = root.join("ai-server").join(".env");
        let existing = fs::read_to_string(&env_path).unwrap_or_default();
        let mut updated_keys = std::collections::HashSet::new();
        let mut out_lines = Vec::new();

        for line in existing.lines() {
            let trimmed = line.trim();
            if !trimmed.is_empty() && !trimmed.starts_with('#') {
                if let Some((k, _)) = trimmed.split_once('=') {
                    let key = k.trim();
                    if let Some(new_val) = entries.get(key) {
                        out_lines.push(format!("{key}=\"{}\"", new_val.replace('"', "\\\"")));
                        updated_keys.insert(key.to_string());
                        continue;
                    }
                }
            }
            out_lines.push(line.to_string());
        }

        for (key, val) in &entries {
            if !updated_keys.contains(key) && !val.is_empty() {
                out_lines.push(format!("{key}=\"{}\"", val.replace('"', "\\\"")));
            }
        }

        fs::write(&env_path, out_lines.join("\n") + "\n")
            .map_err(|e| format!("写入 .env 失败: {e}"))
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn list_history(
    limit: i64,
    filter_path: String,
    query: String,
) -> Result<Vec<HistoryItem>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        let db = db_path(&root);
        if !db.exists() {
            return Ok(Vec::new());
        }
        let conn = Connection::open(&db).map_err(|e| format!("打开数据库失败: {e}"))?;
        let _ = conn.busy_timeout(Duration::from_millis(1000));
        let like_q = format!("%{}%", query.trim());
        let sql = match filter_path.as_str() {
            "error" => {
                "SELECT id, ts, title, options, q_type, answer, analysis, path FROM history \
                 WHERE path = 'error' AND (title LIKE ?1 OR analysis LIKE ?1) ORDER BY id DESC LIMIT ?2"
            }
            "hit" => {
                "SELECT id, ts, title, options, q_type, answer, analysis, path FROM history \
                 WHERE path IN ('local', 'cache') AND (title LIKE ?1 OR answer LIKE ?1) ORDER BY id DESC LIMIT ?2"
            }
            "ai" => {
                "SELECT id, ts, title, options, q_type, answer, analysis, path FROM history \
                 WHERE path IN ('ai', 'bank+ai', 'bank') AND (title LIKE ?1 OR answer LIKE ?1) ORDER BY id DESC LIMIT ?2"
            }
            _ => {
                "SELECT id, ts, title, options, q_type, answer, analysis, path FROM history \
                 WHERE (title LIKE ?1 OR answer LIKE ?1 OR analysis LIKE ?1) ORDER BY id DESC LIMIT ?2"
            }
        };
        let mut stmt = conn.prepare(sql).map_err(|e| format!("查询失败: {e}"))?;
        let rows = stmt
            .query_map(params![like_q, limit], |r| {
                let ts: f64 = r.get(1)?;
                let time_str = Local
                    .timestamp_opt(ts as i64, 0)
                    .single()
                    .map(|dt| dt.format("%m-%d %H:%M").to_string())
                    .unwrap_or_default();
                Ok(HistoryItem {
                    id: r.get(0)?,
                    time_str,
                    title: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    options: r.get::<_, Option<String>>(3)?.unwrap_or_default(),
                    q_type: r.get::<_, Option<String>>(4)?.unwrap_or_default(),
                    answer: r.get::<_, Option<String>>(5)?.unwrap_or_default(),
                    analysis: r.get::<_, Option<String>>(6)?.unwrap_or_default(),
                    path: r.get::<_, Option<String>>(7)?.unwrap_or_default(),
                })
            })
            .map_err(|e| format!("读取记录失败: {e}"))?;

        Ok(rows.filter_map(Result::ok).collect())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn list_local_bank(query: String, limit: i64) -> Result<Vec<LocalBankItem>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        let db = db_path(&root);
        if !db.exists() {
            return Ok(Vec::new());
        }
        let conn = Connection::open(&db).map_err(|e| format!("打开数据库失败: {e}"))?;
        let _ = conn.busy_timeout(Duration::from_millis(1000));
        let q = format!("%{}%", query.trim());
        let mut stmt = conn
            .prepare(
                "SELECT key, title, options, q_type, answer, answer_text, analysis, updated FROM local_bank \
                 WHERE title LIKE ?1 OR answer_text LIKE ?1 OR analysis LIKE ?1 ORDER BY updated DESC LIMIT ?2",
            )
            .map_err(|e| format!("查询本地题库失败: {e}"))?;
        let rows = stmt
            .query_map(params![q, limit], |r| {
                let updated: f64 = r.get(7).unwrap_or(0.0);
                let updated_str = Local
                    .timestamp_opt(updated as i64, 0)
                    .single()
                    .map(|dt| dt.format("%m-%d %H:%M").to_string())
                    .unwrap_or_default();
                Ok(LocalBankItem {
                    key: r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                    title: r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    options: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    q_type: r.get::<_, Option<String>>(3)?.unwrap_or_default(),
                    answer: r.get::<_, Option<String>>(4)?.unwrap_or_default(),
                    answer_text: r.get::<_, Option<String>>(5)?.unwrap_or_default(),
                    analysis: r.get::<_, Option<String>>(6)?.unwrap_or_default(),
                    updated_str,
                })
            })
            .map_err(|e| format!("读取本地题库失败: {e}"))?;

        Ok(rows.filter_map(Result::ok).collect())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn save_local_bank_fix(
    title: String,
    options: String,
    q_type: String,
    answer_text: String,
    analysis: String,
) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        let db = db_path(&root);
        if let Some(parent) = db.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("创建目录失败: {e}"))?;
        }
        let conn = Connection::open(&db).map_err(|e| format!("打开数据库失败: {e}"))?;
        let _ = conn.busy_timeout(Duration::from_millis(1000));
        conn.execute(
            "CREATE TABLE IF NOT EXISTS local_bank (
                key TEXT PRIMARY KEY, title TEXT NOT NULL, options TEXT, q_type TEXT,
                answer TEXT, answer_text TEXT, analysis TEXT, updated REAL NOT NULL
            )",
            [],
        )
        .map_err(|e| format!("建表失败: {e}"))?;

        let key = sha256_hex(&norm_text(&title));
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        let final_analysis = if analysis.trim().is_empty() {
            "已人工修正".to_string()
        } else {
            analysis.trim().to_string()
        };

        conn.execute(
            "INSERT OR REPLACE INTO local_bank (key, title, options, q_type, answer, answer_text, analysis, updated)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![key, title, options, q_type, answer_text, answer_text, final_analysis, now],
        )
        .map_err(|e| format!("写入本地题库失败: {e}"))?;

        let target_norm = norm_text(&title);
        if let Ok(mut stmt) = conn.prepare("SELECT key, title FROM answers") {
            let keys_to_del: Vec<String> = stmt
                .query_map([], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    ))
                })
                .ok()
                .into_iter()
                .flatten()
                .flatten()
                .filter(|(_, t)| norm_text(t) == target_norm)
                .map(|(k, _)| k)
                .collect();
            for k in keys_to_del {
                let _ = conn.execute("DELETE FROM answers WHERE key = ?1", params![k]);
            }
        }

        Ok(())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn delete_history_item(id: i64, title: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        let db = db_path(&root);
        if !db.exists() {
            return Ok("数据库不存在".to_string());
        }
        let conn = Connection::open(&db).map_err(|e| format!("打开数据库失败: {e}"))?;
        let _ = conn.busy_timeout(Duration::from_millis(1000));
        conn.execute("DELETE FROM history WHERE id = ?1", params![id])
            .map_err(|e| format!("删除作答记录失败: {e}"))?;

        let target_norm = norm_text(&title);
        let mut removed_cache = 0;
        if !target_norm.is_empty() {
            if let Ok(mut stmt) = conn.prepare("SELECT key, title FROM answers") {
                let keys_to_del: Vec<String> = stmt
                    .query_map([], |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                        ))
                    })
                    .ok()
                    .into_iter()
                    .flatten()
                    .flatten()
                    .filter(|(_, t)| norm_text(t) == target_norm)
                    .map(|(k, _)| k)
                    .collect();
                for k in keys_to_del {
                    if conn.execute("DELETE FROM answers WHERE key = ?1", params![k]).is_ok() {
                        removed_cache += 1;
                    }
                }
            }
        }
        if removed_cache > 0 {
            Ok(format!("已删除记录并清理 {removed_cache} 条同题缓存"))
        } else {
            Ok("已删除该条作答记录".to_string())
        }
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn delete_local_bank_item(key: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        let db = db_path(&root);
        if !db.exists() {
            return Ok(());
        }
        let conn = Connection::open(&db).map_err(|e| format!("打开数据库失败: {e}"))?;
        conn.execute("DELETE FROM local_bank WHERE key = ?1", params![key])
            .map_err(|e| format!("删除失败: {e}"))?;
        Ok(())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn get_api_balance() -> Result<BalanceInfo, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let root = find_project_root();
        let env_path = root.join("ai-server").join(".env");
        let content = fs::read_to_string(&env_path).unwrap_or_default();
        let mut api_key = String::new();
        let mut base_url = String::new();
        for line in content.lines() {
            let t = line.trim();
            if t.is_empty() || t.starts_with('#') {
                continue;
            }
            if let Some((k, v)) = t.split_once('=') {
                let key = k.trim();
                let val = v.trim().trim_matches('"').trim_matches('\'').to_string();
                if key == "OPENAI_API_KEY" {
                    api_key = val;
                } else if key == "OPENAI_BASE_URL" {
                    base_url = val;
                }
            }
        }
        if api_key.is_empty() {
            return Ok(BalanceInfo {
                supported: false,
                is_available: false,
                currency: "CNY".to_string(),
                total_balance: "-".to_string(),
                granted_balance: "0".to_string(),
                topped_up_balance: "0".to_string(),
                message: "未配置 API Key".to_string(),
            });
        }
        if !base_url.is_empty() && !base_url.contains("deepseek.com") {
            return Ok(BalanceInfo {
                supported: false,
                is_available: true,
                currency: "CNY".to_string(),
                total_balance: "-".to_string(),
                granted_balance: "0".to_string(),
                topped_up_balance: "0".to_string(),
                message: "仅 DeepSeek 官方接口支持余额查询".to_string(),
            });
        }
        let mut cmd = Command::new("curl");
        cmd.args([
            "-s",
            "-m",
            "6",
            "-H",
            &format!("Authorization: Bearer {api_key}"),
            "-H",
            "Accept: application/json",
            "https://api.deepseek.com/user/balance",
        ])
        .stdin(Stdio::null());
        hide_window_if_windows(&mut cmd);

        let output = cmd.output().map_err(|e| format!("请求余额失败: {e}"))?;
        if !output.status.success() {
            return Err("查询 DeepSeek 余额超时或网络失败".to_string());
        }
        let body = String::from_utf8_lossy(&output.stdout);
        let json: serde_json::Value =
            serde_json::from_str(&body).map_err(|_| format!("余额响应解析失败: {body}"))?;
        let is_available = json["is_available"].as_bool().unwrap_or(false);
        let infos = json["balance_infos"].as_array();
        if let Some(first) = infos.and_then(|arr| arr.first()) {
            let currency = first["currency"].as_str().unwrap_or("CNY").to_string();
            let total_balance = first["total_balance"].as_str().unwrap_or("0.00").to_string();
            let granted_balance = first["granted_balance"].as_str().unwrap_or("0.00").to_string();
            let topped_up_balance = first["topped_up_balance"].as_str().unwrap_or("0.00").to_string();
            Ok(BalanceInfo {
                supported: true,
                is_available,
                currency,
                total_balance,
                granted_balance,
                topped_up_balance,
                message: if is_available { "正常" } else { "余额不足" }.to_string(),
            })
        } else {
            Err(format!("DeepSeek 返回异常: {body}"))
        }
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn reset_usage_stats() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let root = find_project_root();
        let db = db_path(&root);
        if !db.exists() {
            return Ok("暂无消耗记录".to_string());
        }
        let conn = Connection::open(&db).map_err(|e| format!("打开数据库失败: {e}"))?;
        let _ = conn.busy_timeout(Duration::from_millis(1000));
        let _ = conn.execute("DELETE FROM usage_logs", []);
        Ok("已重置 Token 与成本统计".to_string())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn clear_answer_cache() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let root = find_project_root();
        let db = db_path(&root);
        if !db.exists() {
            return Ok("缓存本来就是空的".to_string());
        }
        let conn = Connection::open(&db).map_err(|e| format!("打开数据库失败: {e}"))?;
        let _ = conn.busy_timeout(Duration::from_millis(1000));
        let affected = conn
            .execute("DELETE FROM answers", [])
            .map_err(|e| format!("清空缓存失败: {e}"))?;
        Ok(format!("已清空 {affected} 条答案缓存（作答历史已保留）"))
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

fn run_uv_script(root: &Path, args: &[&str]) -> Result<String, String> {
    let ai_dir = root.join("ai-server");
    let mut cmd = Command::new("uv");
    cmd.current_dir(&ai_dir)
        .env("PATH", enhanced_path())
        .arg("run")
        .arg("--locked")
        .arg("python")
        .args(args)
        .stdin(Stdio::null());
    hide_window_if_windows(&mut cmd);

    let output = cmd.output().map_err(|e| format!("调用 Python 脚本失败: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if output.status.success() {
        Ok(if stdout.is_empty() { "完成".to_string() } else { stdout })
    } else {
        Err(if stderr.is_empty() { stdout } else { stderr })
    }
}

#[tauri::command]
async fn import_bank_file(path: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        run_uv_script(&root, &["导入题库.py", "--cache", &path])
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn apply_fix_csv(path: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        run_uv_script(&root, &["应用修正.py", &path])
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

#[tauri::command]
async fn export_csv_file(path: String, mode: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = find_project_root();
        match mode.as_str() {
            "history" => run_uv_script(&root, &["导出题库.py", "--history", &path]),
            "errors" => run_uv_script(&root, &["导出题库.py", "--errors", &path]),
            _ => run_uv_script(&root, &["导出题库.py", &path]),
        }
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?
}

const INSTANCE_SOCK: &str = "/tmp/ai-tiku-desktop.sock";

fn notify_existing_instance() -> bool {
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::net::UnixStream;
        if let Ok(mut stream) = UnixStream::connect(INSTANCE_SOCK) {
            let _ = stream.write_all(b"SHOW");
            return true;
        }
    }
    false
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    if notify_existing_instance() {
        return;
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            #[cfg(unix)]
            {
                use std::io::Read;
                use std::os::unix::net::UnixListener;
                let _ = fs::remove_file(INSTANCE_SOCK);
                if let Ok(listener) = UnixListener::bind(INSTANCE_SOCK) {
                    let handle = app.handle().clone();
                    std::thread::spawn(move || {
                        for stream in listener.incoming().flatten() {
                            let mut s = stream;
                            let mut buf = [0u8; 16];
                            if let Ok(n) = s.read(&mut buf) {
                                if n > 0 {
                                    if let Some(w) = handle.get_webview_window("main") {
                                        let _ = w.show();
                                        let _ = w.unminimize();
                                        let _ = w.set_focus();
                                    }
                                }
                            }
                        }
                    });
                }
            }

            let show_item = MenuItemBuilder::with_id("show", "显示主界面").build(app)?;
            let start_item = MenuItemBuilder::with_id("start", "启动服务").build(app)?;
            let stop_item = MenuItemBuilder::with_id("stop", "停止服务").build(app)?;
            let quit_item = MenuItemBuilder::with_id("quit", "退出软件").build(app)?;
            let menu = MenuBuilder::new(app)
                .items(&[&show_item, &start_item, &stop_item, &quit_item])
                .build()?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "start" => {
                        tauri::async_runtime::spawn_blocking(start_services_sync);
                    }
                    "stop" => {
                        tauri::async_runtime::spawn_blocking(stop_services_sync);
                    }
                    "quit" => {
                        let _ = fs::remove_file(INSTANCE_SOCK);
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            start_services,
            stop_services,
            read_logs,
            clear_logs,
            get_ocs_config,
            open_external,
            read_env_config,
            save_env_config,
            list_history,
            delete_history_item,
            list_local_bank,
            save_local_bank_fix,
            delete_local_bank_item,
            clear_answer_cache,
            reset_usage_stats,
            get_api_balance,
            import_bank_file,
            apply_fix_csv,
            export_csv_file
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
