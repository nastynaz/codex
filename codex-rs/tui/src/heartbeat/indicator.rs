//! Saved schedule state for the displayed thread's footer. Never holds a lock across frames.
use super::Store;
use super::store::Status;
use ratatui::style::Stylize;
use ratatui::text::Line;
use ratatui::text::Span;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Indicator {
    Active { interval_seconds: i64, repeat: bool },
    Dispatching,
    Paused,
    NeedsAttention,
    Disconnected,
}

impl Indicator {
    pub(crate) fn line(&self) -> Line<'static> {
        let label = match self {
            Self::Active {
                interval_seconds,
                repeat,
            } => {
                let cadence = if interval_seconds % 3600 == 0 {
                    format!("{}h", interval_seconds / 3600)
                } else {
                    format!("{}m", interval_seconds / 60)
                };
                if *repeat {
                    format!("● Heartbeat · every {cadence}")
                } else {
                    "● Heartbeat · one-shot".to_string()
                }
            }
            Self::Dispatching => "● Heartbeat · sending".to_string(),
            Self::Paused => "Heartbeat paused".to_string(),
            Self::NeedsAttention => "Heartbeat needs attention".to_string(),
            Self::Disconnected => "Heartbeat disconnected".to_string(),
        };
        let span = Span::raw(label);
        let span = match self {
            Self::Active { .. } | Self::Dispatching => span.light_cyan().bold(),
            Self::Paused | Self::NeedsAttention => span.yellow(),
            Self::Disconnected => span.dark_gray(),
        };
        Line::from(vec![Span::raw("  "), span])
    }
}

impl Store {
    pub(crate) fn indicator(&self, thread: &str, connected: bool) -> Option<Indicator> {
        self.read_indicator(thread, connected)
            .unwrap_or(Some(Indicator::NeedsAttention))
    }

    fn read_indicator(&self, thread: &str, connected: bool) -> Result<Option<Indicator>, String> {
        // Avoid creating lock files for threads that have never had a heartbeat.
        if !self.has_schedule(thread)? {
            return Ok(None);
        }
        let Some(locked) = self.lock(thread)? else {
            return Ok(Some(if connected {
                Indicator::Dispatching
            } else {
                Indicator::Disconnected
            }));
        };
        let Some(schedule) = locked.read()? else {
            return Ok(None);
        };
        Ok(Some(if schedule.last_error.is_some() {
            Indicator::NeedsAttention
        } else if schedule.status == Status::Paused {
            Indicator::Paused
        } else if !connected {
            Indicator::Disconnected
        } else {
            Indicator::Active {
                interval_seconds: schedule.interval_seconds,
                repeat: schedule.repeat,
            }
        }))
    }
}
