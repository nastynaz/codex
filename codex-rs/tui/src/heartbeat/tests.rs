use super::*;
use serde_json::json;
use std::sync::Arc;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;

const THREAD: &str = "019c6e27-e55b-73d1-87d8-4e01f1f75043";

fn create(store: &Store) -> Value {
    tool::execute(
        store,
        THREAD,
        json!({"mode":"create","kind":"heartbeat","destination":"thread",
        "name":"Check work","prompt":"Check the work. Delete the heartbeat when finished.",
        "rrule":"FREQ=MINUTELY;INTERVAL=5","status":"ACTIVE"}),
        1000,
    )
    .unwrap()
}

fn saved(store: &Store) -> store::Schedule {
    store
        .lock(THREAD)
        .unwrap()
        .unwrap()
        .read()
        .unwrap()
        .unwrap()
}

#[test]
fn heartbeat_tool_lifecycle_is_persistent_and_scoped() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    let created = create(&store);
    assert_eq!(create(&store)["status"], "unchanged");
    assert_eq!(saved(&store).next_run_at, 1300);
    assert!(saved(&store).repeat);
    let reopened = Store::new(directory.path());
    assert_eq!(
        tool::execute(&reopened, THREAD, json!({"mode":"view"}), 1100).unwrap()["automation"],
        created["automation"]
    );
    let paused = tool::execute(
        &store,
        THREAD,
        json!({"mode":"update","status":"PAUSED"}),
        1100,
    )
    .unwrap();
    assert_eq!(
        paused["automation"]["prompt"],
        created["automation"]["prompt"]
    );
    let another = uuid::Uuid::new_v4().to_string();
    assert!(
        tool::execute(
            &store,
            &another,
            json!({"mode":"delete","id":created["automation"]["id"]}),
            1100
        )
        .is_err()
    );
    tool::execute(
        &store,
        THREAD,
        json!({"mode":"update","status":"ACTIVE","rrule":"FREQ=HOURLY;INTERVAL=2"}),
        1200,
    )
    .unwrap();
    assert_eq!(saved(&store).next_run_at, 8400);
    tool::execute(&store, THREAD, json!({"mode":"delete"}), 1200).unwrap();
    tool::execute(&store, THREAD, json!({"mode":"delete"}), 1200).unwrap();
    assert!(
        tool::execute(&store, THREAD, json!({"mode":"view"}), 1200).unwrap()["automation"]
            .is_null()
    );
}

#[test]
fn heartbeat_rejects_invalid_rules_and_arguments_without_mutation() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    create(&store);
    let before = saved(&store);
    assert!(tool::interval(&format!("FREQ=MINUTELY;INTERVAL={}1", "0".repeat(100))).is_err());
    for rule in [
        "FREQ=SECONDLY;INTERVAL=1",
        "FREQ=MINUTELY;INTERVAL=0",
        "FREQ=HOURLY;INTERVAL=25",
        "FREQ=MINUTELY;INTERVAL=5;COUNT=2",
        "FREQ=MINUTELY;INTERVAL=1;INTERVAL=2",
        "FREQ=MINUTELY;INTERVAL=-1",
        "FREQ=MINUTELY;INTERVAL=9223372036854775807",
    ] {
        assert!(
            tool::execute(&store, THREAD, json!({"mode":"update","rrule":rule}), 1200).is_err(),
            "{rule}"
        );
        assert_eq!(saved(&store), before);
    }
    assert!(
        tool::execute(
            &store,
            THREAD,
            json!({"mode":"view","prompt":"ignored?"}),
            1200
        )
        .is_err()
    );
    assert!(
        tool::execute(
            &store,
            THREAD,
            json!({"mode":"update","threadId":"another"}),
            1200
        )
        .is_err()
    );
    assert!(store.lock("../escape").is_err());
}

#[tokio::test]
async fn heartbeat_due_tick_delivers_once_and_missed_ticks_do_not_accumulate() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    create(&store);
    run_due(&store, THREAD, 1299, |_| async { panic!("early dispatch") })
        .await
        .unwrap();
    run_due(&store, THREAD, 1300, |_| async {
        Ok(Outcome::Accepted("turn-1".into()))
    })
    .await
    .unwrap();
    assert_eq!(saved(&store).last_turn_id.as_deref(), Some("turn-1"));
    run_due(&store, THREAD, 1300, |_| async {
        panic!("duplicate dispatch")
    })
    .await
    .unwrap();
    run_due(&store, THREAD, 1600, |_| async {
        Ok(Outcome::Accepted("turn-1".into()))
    })
    .await
    .unwrap();
    assert_eq!(saved(&store).next_run_at, 1900);
    assert_eq!(saved(&store).last_run_at, Some(1600));
    run_due(&store, THREAD, 5000, |_| async {
        panic!("replayed missed tick")
    })
    .await
    .unwrap();
    assert_eq!(saved(&store).next_run_at, 5300);
}

#[tokio::test]
async fn heartbeat_clients_share_a_dispatch_lock() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    create(&store);
    let other = Store::new(directory.path());
    let calls = Arc::new(AtomicUsize::new(0));
    let (entered, started) = tokio::sync::oneshot::channel();
    let (release, released) = tokio::sync::oneshot::channel();
    let calls1 = calls.clone();
    let first = run_due(&store, THREAD, 1300, |_| async move {
        calls1.fetch_add(1, Ordering::SeqCst);
        entered.send(()).unwrap();
        released.await.unwrap();
        Ok(Outcome::Accepted("first".into()))
    });
    let second = async {
        started.await.unwrap();
        run_due(&other, THREAD, 1300, |_| async {
            panic!("second owner dispatched")
        })
        .await
        .unwrap();
        assert!(tool::execute(&other, THREAD, json!({"mode":"delete"}), 1300).is_err());
        release.send(()).unwrap();
    };
    let (result, ()) = tokio::join!(first, second);
    result.unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(saved(&store).last_turn_id.as_deref(), Some("first"));
}

#[tokio::test]
async fn heartbeat_uncertain_dispatch_survives_cancellation_as_paused() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    create(&store);
    let copy = store.clone();
    let (entered, started) = tokio::sync::oneshot::channel();
    let task = tokio::spawn(async move {
        run_due(&copy, THREAD, 1300, |_| async move {
            entered.send(()).unwrap();
            std::future::pending().await
        })
        .await
    });
    started.await.unwrap();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert_eq!(saved(&store).status, Status::Paused);
    assert!(
        run_due(&store, THREAD, 2000, |_| async {
            panic!("replayed uncertain request")
        })
        .await
        .unwrap()
        .is_some()
    );
    tool::execute(
        &store,
        THREAD,
        json!({"mode":"update","status":"ACTIVE"}),
        2000,
    )
    .unwrap();
    assert_eq!(saved(&store).next_run_at, 2300);
    assert!(saved(&store).last_error.is_none());
}

#[tokio::test(start_paused = true)]
async fn heartbeat_timeouts_and_failures_pause_until_explicit_resume() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    create(&store);
    let error = run_due(&store, THREAD, 1300, |_| std::future::pending())
        .await
        .unwrap()
        .unwrap();
    assert!(error.contains("timed out"));
    tool::execute(
        &store,
        THREAD,
        json!({"mode":"update","status":"ACTIVE"}),
        1400,
    )
    .unwrap();
    let error = run_due(&store, THREAD, 1700, |_| async {
        Err("method not found".into())
    })
    .await
    .unwrap()
    .unwrap();
    assert!(error.contains("method not found"));
    assert_eq!(saved(&store).status, Status::Paused);
}

#[tokio::test]
async fn heartbeat_completion_deletes_schedule_and_corrupt_files_fail_closed() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    create(&store);
    run_due(&store, THREAD, 1300, |_| async { Ok(Outcome::Completed) })
        .await
        .unwrap();
    assert!(store.threads().unwrap().is_empty());
    let path = directory
        .path()
        .join("cli-heartbeats")
        .join(format!("{THREAD}.json"));
    std::fs::write(&path, "broken json").unwrap();
    assert!(
        run_due(&store, THREAD, 1300, |_| async {
            panic!("corrupt dispatch")
        })
        .await
        .is_err()
    );
    assert_eq!(std::fs::read_to_string(path).unwrap(), "broken json");
    tool::execute(&store, THREAD, json!({"mode":"delete"}), 1300).unwrap();
    create(&store);
}

#[tokio::test]
async fn heartbeat_one_shot_survives_offline_delay_and_deferral_until_accepted() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    let args = json!({"mode":"create","kind":"heartbeat","destination":"thread",
        "name":"Follow up","prompt":"Check the result","rrule":"FREQ=MINUTELY;INTERVAL=5","repeat":false});
    tool::execute(&store, THREAD, args.clone(), 1000).unwrap();
    assert_eq!(
        tool::execute(&store, THREAD, args, 1000).unwrap()["status"],
        "unchanged"
    );
    assert!(!saved(&store).repeat);
    // Defaulting a new creation to repeating must not match this one-shot.
    assert!(
        tool::execute(
            &store,
            THREAD,
            json!({"mode":"create","kind":"heartbeat","destination":"thread",
        "name":"Follow up","prompt":"Check the result","rrule":"FREQ=MINUTELY;INTERVAL=5"}),
            1000
        )
        .is_err()
    );
    let reopened = Store::new(directory.path());
    run_due(&reopened, THREAD, 5000, |schedule| async move {
        let message = dispatch::activation_message(&schedule, 5000).unwrap();
        assert!(message.contains("Heartbeat activated: Follow up"));
        assert!(message.contains("Repeat: false; interval: 300 seconds"));
        assert!(message.contains("one-shot schedule is deleted when the server accepts"));
        assert!(message.contains("Check the result"));
        assert!(message.contains("<current_time_iso>1970-01-01T01:23:20+00:00</current_time_iso>"));
        Ok(Outcome::Deferred)
    })
    .await
    .unwrap();
    assert_eq!(saved(&reopened).status, Status::Active);
    assert_eq!(saved(&reopened).next_run_at, 5030);
    run_due(&reopened, THREAD, 5029, |_| async {
        panic!("retried early")
    })
    .await
    .unwrap();
    run_due(&reopened, THREAD, 5030, |_| async {
        Ok(Outcome::Accepted("already-active-turn".into()))
    })
    .await
    .unwrap();
    assert!(reopened.threads().unwrap().is_empty());
    run_due(&reopened, THREAD, 6000, |_| async {
        panic!("one-shot fired twice")
    })
    .await
    .unwrap();
}

#[test]
fn heartbeat_legacy_schedules_repeat_and_updates_preserve_the_chosen_lifecycle() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    create(&store);
    let path = directory
        .path()
        .join("cli-heartbeats")
        .join(format!("{THREAD}.json"));
    let mut legacy = serde_json::to_value(saved(&store)).unwrap();
    legacy.as_object_mut().unwrap().remove("repeat");
    std::fs::write(&path, serde_json::to_vec(&legacy).unwrap()).unwrap();
    assert!(saved(&store).repeat);
    tool::execute(
        &store,
        THREAD,
        json!({"mode":"update","repeat":false}),
        1100,
    )
    .unwrap();
    tool::execute(
        &store,
        THREAD,
        json!({"mode":"update","name":"Renamed"}),
        1200,
    )
    .unwrap();
    assert!(!saved(&store).repeat);
    assert!(tool::execute(&store, THREAD, json!({"mode":"view","repeat":true}), 1200).is_err());
    assert!(!saved(&store).repeat);
    tool::execute(&store, THREAD, json!({"mode":"update","repeat":true}), 1200).unwrap();
    assert!(saved(&store).repeat);
}
