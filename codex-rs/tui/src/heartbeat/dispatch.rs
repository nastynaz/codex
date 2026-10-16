//! Thin adapter to the existing app-server and TUI lifecycle.
use super::store::Schedule;
use crate::app_event_sender::AppEventSender;
use codex_app_server_client::AppServerRequestHandle;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::ThreadReadParams;
use codex_app_server_protocol::ThreadReadResponse;
use codex_app_server_protocol::ThreadResumeParams;
use codex_app_server_protocol::ThreadResumeResponse;
use codex_app_server_protocol::ThreadStatus;
use codex_app_server_protocol::TurnHeartbeatParams;
use codex_app_server_protocol::TurnHeartbeatResponse;
use codex_app_server_protocol::UserInput;
use codex_features::Features;
use serde::de::DeserializeOwned;
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;

#[derive(Debug, PartialEq)]
pub(super) enum Outcome {
    Accepted(String),
    Deferred,
    Completed,
}

async fn request<T: DeserializeOwned>(
    handle: &AppServerRequestHandle,
    build: impl FnOnce(RequestId) -> ClientRequest,
) -> Result<T, String> {
    handle
        .request_typed(build(RequestId::String(format!(
            "tui-heartbeat-{}",
            uuid::Uuid::new_v4()
        ))))
        .await
        .map_err(|e| e.to_string())
}

pub(super) async fn dispatch(
    schedule: &Schedule,
    now: i64,
    handle: &AppServerRequestHandle,
    events: &AppEventSender,
    features: &Features,
    mcp_config: Value,
) -> Result<Outcome, String> {
    let response: ThreadReadResponse = request(handle, |request_id| ClientRequest::ThreadRead {
        request_id,
        params: ThreadReadParams {
            thread_id: schedule.thread_id.clone(),
            include_turns: false,
        },
    })
    .await?;
    let thread = response.thread;
    if schedule.repeat
        && thread
            .name
            .as_deref()
            .is_some_and(|name| name.starts_with("done:"))
    {
        return Ok(Outcome::Completed);
    }
    if thread.ephemeral
        || thread.parent_thread_id.is_some()
        || matches!(thread.status, ThreadStatus::SystemError)
    {
        return Err("Heartbeat requires a healthy, persistent top-level thread".into());
    }
    // Only refresh this client's tool endpoint. Preserve the target's model, permissions,
    // environments and instructions; never inherit them from whichever thread is in front.
    let resumed: ThreadResumeResponse = request(handle, |request_id| ClientRequest::ThreadResume {
        request_id,
        params: ThreadResumeParams {
            thread_id: schedule.thread_id.clone(),
            exclude_turns: true,
            config: Some(HashMap::from([(
                "mcp_servers.codex_tui".into(),
                mcp_config,
            )])),
            ..Default::default()
        },
    })
    .await?;
    let program = crate::dynamic_tools::background_turn_program(
        features,
        handle,
        &schedule.thread_id,
        &resumed.model,
        &resumed.model_provider,
        thread.daybreak_enabled.unwrap_or(false),
    )
    .await?;
    crate::dynamic_tools::register_background_thread(Some(events), resumed.thread, true).await?;
    let text = activation_message(schedule, now)?;
    let response: TurnHeartbeatResponse =
        request(handle, |request_id| ClientRequest::TurnHeartbeat {
            request_id,
            params: TurnHeartbeatParams {
                thread_id: schedule.thread_id.clone(),
                input: vec![UserInput::Text {
                    text,
                    text_elements: Vec::new(),
                }],
                cyber_access_program: program,
                ..Default::default()
            },
        })
        .await?;
    Ok(response
        .turn
        .map_or(Outcome::Deferred, |turn| Outcome::Accepted(turn.id)))
}

pub(super) fn activation_message(schedule: &Schedule, now: i64) -> Result<String, String> {
    let timestamp = chrono::DateTime::from_timestamp(now, 0)
        .ok_or("Invalid heartbeat timestamp")?
        .to_rfc3339();
    let lifecycle = if schedule.repeat {
        "This repeating schedule remains active until paused or deleted."
    } else {
        "This one-shot schedule is deleted when the server accepts this message."
    };
    // Keep the envelope recognised by history; stable activation context belongs
    // inside instructions so repeated heartbeats can still be coalesced.
    Ok(format!(
        "<heartbeat>\n  <automation_id>{}</automation_id>\n  <current_time_iso>{timestamp}</current_time_iso>\n  <instructions>\nHeartbeat activated: {}\nRepeat: {}; interval: {} seconds.\n{lifecycle}\nThis is a scheduled message. If you are already working, consider it alongside your ongoing work at your next processing opportunity. Existing instructions and approvals still apply.\n\nSaved instructions:\n{}\n  </instructions>\n</heartbeat>\n",
        schedule.id, schedule.name, schedule.repeat, schedule.interval_seconds, schedule.prompt
    ))
}

pub(crate) fn response(
    result: Result<Value, String>,
) -> codex_app_server_protocol::DynamicToolCallResponse {
    let success = result.is_ok();
    let value = result.unwrap_or_else(|error| json!({"status":"error","error":error}));
    codex_app_server_protocol::DynamicToolCallResponse {
        content_items: vec![
            codex_app_server_protocol::DynamicToolCallOutputContentItem::InputText {
                text: value.to_string(),
            },
        ],
        success,
    }
}
