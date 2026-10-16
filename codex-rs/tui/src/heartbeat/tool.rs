use super::store::Schedule;
use super::store::Status;
use super::store::Store;
use codex_app_server_protocol::DynamicToolFunctionSpec;
use codex_app_server_protocol::DynamicToolSpec;
use serde::Deserialize;
use serde_json::Value;
use serde_json::json;

pub(crate) fn spec() -> DynamicToolSpec {
    DynamicToolSpec::Function(DynamicToolFunctionSpec {
        name: "automation_update".into(),
        description: "Create, view, update, pause or delete the calling thread's heartbeat. Runs only while a local Codex TUI is open, with saved thread settings. One schedule per thread. repeat defaults to true; set false for one delivery, automatically deleted after the server accepts its message. Use only for a user-requested loop. Set mode=create, kind=heartbeat, destination=thread, status=ACTIVE, name, prompt and rrule. Supported rules: FREQ=MINUTELY;INTERVAL=N or FREQ=HOURLY;INTERVAL=N (1 minute to 24 hours). First activation is after one interval. Busy agents receive a queued heartbeat message. Missed recurring ticks are skipped; overdue one-shots remain due. For repeating schedules, on completion delete the schedule, then rename the thread done: <name>. Failed/uncertain submissions pause automatically; inspect before explicitly resuming. view returns the full saved state; update preserves omitted fields. Omit id to use this thread's schedule.".into(),
        input_schema: json!({"type":"object","additionalProperties":false,"required":["mode"],"properties":{
            "mode":{"type":"string","enum":["create","view","update","delete"]},
            "kind":{"type":"string","enum":["heartbeat"]},
            "destination":{"type":"string","enum":["thread"]},
            "id":{"type":"string"},
            "status":{"type":"string","enum":["ACTIVE","PAUSED"]},
            "name":{"type":"string","minLength":1,"maxLength":100},
            "prompt":{"type":"string","minLength":1,"maxLength":2000},
            "repeat":{"type":"boolean","default":true},
            "rrule":{"type":"string","maxLength":64}
        }}),
        defer_loading: true,
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Arguments {
    mode: String,
    kind: Option<String>,
    destination: Option<String>,
    id: Option<String>,
    status: Option<Status>,
    name: Option<String>,
    prompt: Option<String>,
    rrule: Option<String>,
    repeat: Option<bool>,
}

pub(super) fn interval(rule: &str) -> Result<i64, String> {
    if rule.len() > 64 {
        return Err("Recurrence rule must be at most 64 bytes".into());
    }
    let mut frequency = None;
    let mut count = None;
    for part in rule.strip_prefix("RRULE:").unwrap_or(rule).split(';') {
        match part.split_once('=') {
            Some(("FREQ", value)) if frequency.is_none() => frequency = Some(value),
            Some(("INTERVAL", value)) if count.is_none() => {
                count = Some(
                    value
                        .parse::<i64>()
                        .map_err(|_| "INTERVAL must be an integer")?,
                )
            }
            _ => return Err("Use FREQ=MINUTELY;INTERVAL=N or FREQ=HOURLY;INTERVAL=N".into()),
        }
    }
    let multiplier = match frequency {
        Some("MINUTELY") => 60,
        Some("HOURLY") => 3600,
        _ => 0,
    };
    let seconds = count.unwrap_or(0).checked_mul(multiplier).unwrap_or(0);
    if !(60..=86400).contains(&seconds) {
        return Err("Heartbeat interval must be 1 minute to 24 hours".into());
    }
    Ok(seconds)
}

pub(super) fn validate_text(name: &str, prompt: &str) -> Result<(), String> {
    if name.trim().is_empty() || name.len() > 100 || name.chars().any(char::is_control) {
        return Err("Name must be 1–100 UTF-8 bytes without control characters".into());
    }
    if prompt.trim().is_empty() || prompt.len() > 2000 {
        return Err("Prompt must be 1–2,000 UTF-8 bytes".into());
    }
    Ok(())
}

pub(super) fn execute(
    store: &Store,
    thread: &str,
    value: Value,
    now: i64,
) -> Result<Value, String> {
    let args: Arguments = serde_json::from_value(value).map_err(|e| e.to_string())?;
    if args.kind.as_deref().is_some_and(|v| v != "heartbeat")
        || args.destination.as_deref().is_some_and(|v| v != "thread")
    {
        return Err("Only thread-attached heartbeats are supported".into());
    }
    if !matches!(args.mode.as_str(), "create" | "view" | "update" | "delete") {
        return Err("Use create, view, update or delete".into());
    }
    if matches!(args.mode.as_str(), "view" | "delete")
        && (args.name.is_some()
            || args.prompt.is_some()
            || args.rrule.is_some()
            || args.status.is_some()
            || args.repeat.is_some())
    {
        return Err("view/delete do not accept schedule changes".into());
    }
    let locked = store
        .lock(thread)?
        .ok_or("Heartbeat dispatch is in progress; retry this tool shortly")?;
    // Explicit deletion of this thread's schedule also works after a corrupt write
    // or a downgrade to a client that cannot understand its file version.
    if args.mode == "delete" && args.id.is_none() {
        locked.delete()?;
        return Ok(json!({"status":"deleted","threadId":thread}));
    }
    let existing = locked.read()?;
    if let Some(id) = &args.id
        && existing.as_ref().is_none_or(|s| &s.id != id)
    {
        return Err("No matching heartbeat in the calling thread; use mode=view without id".into());
    }
    if args.mode == "view" {
        return Ok(json!({"automation":existing,"runsWhile":"local TUI open"}));
    }
    if args.mode == "delete" {
        locked.delete()?;
        return Ok(json!({"status":"deleted","threadId":thread}));
    }
    let mut schedule = if args.mode == "create" {
        if args.kind.as_deref() != Some("heartbeat")
            || args.destination.as_deref() != Some("thread")
        {
            return Err("Creation requires kind=heartbeat and destination=thread".into());
        }
        let name = args.name.ok_or("Creation requires name")?;
        let prompt = args.prompt.ok_or("Creation requires prompt")?;
        let rrule = args.rrule.ok_or("Creation requires rrule")?;
        let status = args.status.unwrap_or(Status::Active);
        let repeat = args.repeat.unwrap_or(true);
        let seconds = interval(&rrule)?;
        validate_text(&name, &prompt)?;
        if let Some(saved) = existing {
            if saved.name == name
                && saved.prompt == prompt
                && saved.interval_seconds == seconds
                && saved.status == status
                && saved.repeat == repeat
            {
                return Ok(json!({"status":"unchanged","automation":saved}));
            }
            return Err("This thread already has a heartbeat; use mode=update".into());
        }
        Schedule {
            version: 1,
            id: uuid::Uuid::new_v4().to_string(),
            thread_id: thread.into(),
            name,
            prompt,
            rrule,
            interval_seconds: seconds,
            repeat,
            status,
            next_run_at: now + seconds,
            last_run_at: None,
            last_turn_id: None,
            last_error: None,
        }
    } else {
        let mut saved = existing.ok_or("No heartbeat in this thread; use mode=create")?;
        if let Some(name) = args.name {
            saved.name = name;
        }
        if let Some(prompt) = args.prompt {
            saved.prompt = prompt;
        }
        if let Some(rule) = args.rrule {
            saved.interval_seconds = interval(&rule)?;
            saved.rrule = rule;
        }
        if let Some(repeat) = args.repeat {
            saved.repeat = repeat;
        }
        if let Some(status) = args.status {
            saved.status = status;
        }
        saved.next_run_at = now + saved.interval_seconds;
        saved
    };
    validate_text(&schedule.name, &schedule.prompt)?;
    schedule.last_error = None;
    locked.save(&schedule)?;
    let next = if schedule.repeat {
        "On completion use mode=delete, then rename this thread done: <name>."
    } else {
        "The schedule is deleted after its single heartbeat message is accepted. A deferred delivery stays pending."
    };
    Ok(
        json!({"status":"saved","automation":schedule,"runsWhile":"local TUI open",
        "next":next}),
    )
}
