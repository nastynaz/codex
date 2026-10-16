//! Private, atomic schedule files and cross-client locks. No changes to Codex's database.
use serde::Deserialize;
use serde::Serialize;
use std::fs::File;
use std::fs::OpenOptions;
use std::fs::TryLockError;
use std::io::Read;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct Schedule {
    pub version: u32,
    pub id: String,
    pub thread_id: String,
    pub name: String,
    pub prompt: String,
    pub rrule: String,
    pub interval_seconds: i64,
    #[serde(default = "default_repeat")]
    pub repeat: bool,
    pub status: Status,
    pub next_run_at: i64,
    pub last_run_at: Option<i64>,
    pub last_turn_id: Option<String>,
    pub last_error: Option<String>,
}

fn default_repeat() -> bool {
    true
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(super) enum Status {
    Active,
    Paused,
}

#[derive(Clone)]
pub(crate) struct Store {
    root: PathBuf,
}

pub(super) struct LockedSchedule {
    _lock: File,
    path: PathBuf,
}

impl Store {
    pub(crate) fn new(codex_home: &Path) -> Self {
        Self {
            root: codex_home.join("cli-heartbeats"),
        }
    }

    pub(super) fn lock(&self, thread_id: &str) -> Result<Option<LockedSchedule>, String> {
        uuid::Uuid::parse_str(thread_id).map_err(|_| "Invalid calling thread ID")?;
        let mut builder = std::fs::DirBuilder::new();
        builder.recursive(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        builder.create(&self.root).map_err(|e| e.to_string())?;
        let mut options = OpenOptions::new();
        options.create(true).read(true).write(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let lock = options
            .open(self.root.join(format!("{thread_id}.lock")))
            .map_err(|e| e.to_string())?;
        match lock.try_lock() {
            Ok(()) => Ok(Some(LockedSchedule {
                _lock: lock,
                path: self.root.join(format!("{thread_id}.json")),
            })),
            Err(TryLockError::WouldBlock) => Ok(None),
            Err(TryLockError::Error(e)) => Err(e.to_string()),
        }
    }

    pub(super) fn threads(&self) -> Result<Vec<String>, String> {
        let entries = match std::fs::read_dir(&self.root) {
            Ok(entries) => entries,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(e.to_string()),
        };
        let mut threads = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|e| e.to_string())?;
            let path = entry.path();
            if path.extension().is_some_and(|ext| ext == "json")
                && let Some(id) = path.file_stem().and_then(|stem| stem.to_str())
                && uuid::Uuid::parse_str(id).is_ok()
            {
                threads.push(id.to_string());
            }
        }
        threads.sort();
        Ok(threads)
    }
}

impl LockedSchedule {
    pub(super) fn read(&self) -> Result<Option<Schedule>, String> {
        let file = match File::open(&self.path) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.to_string()),
        };
        let mut bytes = Vec::new();
        file.take(16_385)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        if bytes.len() > 16_384 {
            return Err("Heartbeat file exceeds 16 KiB".into());
        }
        let schedule: Schedule = serde_json::from_slice(&bytes).map_err(|e| format!("Invalid heartbeat file: {e}. Use mode=delete without id, then recreate the schedule"))?;
        if schedule.version != 1
            || self.path.file_stem().and_then(|s| s.to_str()) != Some(&schedule.thread_id)
            || uuid::Uuid::parse_str(&schedule.id).is_err()
            || super::tool::interval(&schedule.rrule)? != schedule.interval_seconds
        {
            return Err("Invalid or unsupported heartbeat file. Use mode=delete without id, then recreate the schedule".into());
        }
        super::tool::validate_text(&schedule.name, &schedule.prompt)?;
        Ok(Some(schedule))
    }

    pub(super) fn save(&self, schedule: &Schedule) -> Result<(), String> {
        let parent = self.path.parent().ok_or("Missing schedule directory")?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
        serde_json::to_writer_pretty(&mut temporary, schedule).map_err(|e| e.to_string())?;
        temporary.write_all(b"\n").map_err(|e| e.to_string())?;
        temporary.as_file().sync_all().map_err(|e| e.to_string())?;
        temporary.persist(&self.path).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        File::open(parent)
            .and_then(|directory| directory.sync_all())
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    pub(super) fn delete(&self) -> Result<(), String> {
        match std::fs::remove_file(&self.path) {
            Ok(()) => {
                #[cfg(unix)]
                File::open(self.path.parent().ok_or("Missing schedule directory")?)
                    .and_then(|directory| directory.sync_all())
                    .map_err(|e| e.to_string())?;
                Ok(())
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    }
}
