//! Client-owned heartbeats. Dropping the bridge stops its timer; schedule files survive.
mod dispatch;
mod store;
mod tool;

use crate::app_event::AppEvent;
use crate::dynamic_tools_mcp::ToolConnection;
use codex_features::Features;
use dispatch::Outcome;
use serde_json::Value;
use std::collections::HashMap;
use std::future::Future;
use std::sync::PoisonError;
use std::time::Duration;
use store::Status;
pub(crate) use store::Store;
use tokio::task::JoinHandle;

pub(crate) struct Scheduler {
    task: JoinHandle<()>,
}

impl Drop for Scheduler {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl Store {
    pub(crate) fn spec(&self) -> codex_app_server_protocol::DynamicToolSpec {
        tool::spec()
    }

    pub(crate) fn execute(
        &self,
        thread: &str,
        arguments: Value,
    ) -> codex_app_server_protocol::DynamicToolCallResponse {
        dispatch::response(tool::execute(
            self,
            thread,
            arguments,
            chrono::Utc::now().timestamp(),
        ))
    }

    pub(crate) fn start(
        &self,
        connection: ToolConnection,
        features: Features,
        mcp_config: Value,
    ) -> Scheduler {
        let store = self.clone();
        let task = tokio::spawn(async move {
            let mut timer = tokio::time::interval(Duration::from_secs(1));
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let mut warnings = HashMap::<String, String>::new();
            loop {
                timer.tick().await;
                let Some((_handle, events)) = connection
                    .read()
                    .unwrap_or_else(PoisonError::into_inner)
                    .clone()
                else {
                    continue;
                };
                let threads = match store.threads() {
                    Ok(threads) => threads,
                    Err(error) => {
                        warn_once(&mut warnings, "storage", &error, &events);
                        continue;
                    }
                };
                warnings.remove("storage");
                warnings.retain(|key, _| threads.contains(key));
                for thread in threads {
                    // Check again between dispatches; never send on a known suspended connection.
                    let Some((handle, events)) = connection
                        .read()
                        .unwrap_or_else(PoisonError::into_inner)
                        .clone()
                    else {
                        break;
                    };
                    let now = chrono::Utc::now().timestamp();
                    let result = run_due(&store, &thread, now, |schedule| {
                        let handle = handle.clone();
                        let events = events.clone();
                        let features = features.clone();
                        let config = mcp_config.clone();
                        async move {
                            dispatch::dispatch(&schedule, now, &handle, &events, &features, config)
                                .await
                        }
                    })
                    .await;
                    match result {
                        Ok(Some(message)) | Err(message) => {
                            warn_once(&mut warnings, &thread, &message, &events)
                        }
                        Ok(None) => {
                            warnings.remove(&thread);
                        }
                    }
                }
            }
        });
        Scheduler { task }
    }
}

fn warn_once(
    warnings: &mut HashMap<String, String>,
    thread: &str,
    error: &str,
    events: &crate::app_event_sender::AppEventSender,
) {
    if warnings
        .get(thread)
        .is_some_and(|previous| previous == error)
    {
        return;
    }
    warnings.insert(thread.into(), error.into());
    tracing::warn!(thread_id = thread, %error, "Heartbeat needs attention");
    events.send(AppEvent::InsertHistoryCell(Box::new(crate::history_cell::new_warning_event(
        format!("Heartbeat [{thread}]: {error}. Open that thread and inspect its heartbeat before resuming.")
    ))));
}

async fn run_due<F, Fut>(
    store: &Store,
    thread: &str,
    now: i64,
    dispatch: F,
) -> Result<Option<String>, String>
where
    F: FnOnce(store::Schedule) -> Fut,
    Fut: Future<Output = Result<Outcome, String>>,
{
    let Some(locked) = store.lock(thread)? else {
        return Ok(None);
    };
    let Some(mut schedule) = locked.read()? else {
        return Ok(None);
    };
    if schedule.status == Status::Paused {
        return Ok(schedule.last_error);
    }
    if schedule.next_run_at > now {
        return Ok(None);
    }
    let missed = now.saturating_sub(schedule.next_run_at) >= schedule.interval_seconds;
    schedule.next_run_at = now + schedule.interval_seconds;
    if missed && schedule.repeat {
        locked.save(&schedule)?;
        return Ok(None);
    }
    // Write before sending. A crash, cancellation or lost reply leaves the schedule paused,
    // never replaying an uncertain request. The cross-client lock stays held until resolution.
    schedule.status = Status::Paused;
    schedule.last_error =
        Some("Previous dispatch did not finish; its outcome may be unknown".into());
    locked.save(&schedule)?;
    let result = tokio::time::timeout(Duration::from_secs(30), dispatch(schedule.clone())).await;
    match result {
        Ok(Ok(Outcome::Accepted(turn))) => {
            if !schedule.repeat {
                locked.delete()?;
                return Ok(None);
            }
            schedule.status = Status::Active;
            schedule.last_run_at = Some(now);
            schedule.last_turn_id = Some(turn);
            schedule.last_error = None;
        }
        Ok(Ok(Outcome::Deferred)) => {
            schedule.next_run_at = now + 30;
            schedule.status = Status::Active;
            schedule.last_error = None;
        }
        Ok(Ok(Outcome::Completed)) => {
            locked.delete()?;
            return Ok(None);
        }
        Ok(Err(error)) => {
            schedule.last_error = Some(format!(
                "Paused after dispatch failure: {}",
                bounded_error(&error)
            ));
        }
        Err(_) => {
            schedule.last_error = Some("Paused: dispatch timed out; inspect thread history because a turn may have started".into());
        }
    }
    locked.save(&schedule)?;
    Ok(schedule.last_error)
}

fn bounded_error(error: &str) -> String {
    error.chars().take(500).collect()
}

#[cfg(test)]
mod tests;
