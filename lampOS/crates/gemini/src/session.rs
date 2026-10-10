use crate::{
    BarrierPolicy, Discard, EVENT_QUEUE_CAPACITY, Error, Event, INPUT_QUEUE_CAPACITY, Lineage,
    MAX_INPUT_AGE, MAX_INPUT_SAMPLES, MAX_WIRE_BYTES, OUTPUT_RATE, RequestId, Result, Resumption,
    ResumptionHandle, SessionConfig, SessionId, State, Timeouts, wire,
};
use futures_util::{SinkExt, StreamExt};
use std::{
    collections::VecDeque,
    sync::{
        Arc, Mutex, PoisonError,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
// Deadlines use the runtime clock so scripted tests can cover minutes of
// conversation without waiting for them. Capture ages stay on the system
// monotonic clock: they describe when audio was actually acquired.
use tokio::{
    io::{AsyncRead, AsyncWrite},
    sync::{Notify, mpsc, watch},
    task::JoinHandle,
    time::{Instant as Clock, MissedTickBehavior, timeout},
};
use tokio_tungstenite::{
    WebSocketStream, connect_async_with_config,
    tungstenite::{self, Message, protocol::WebSocketConfig},
};

#[derive(Clone, Copy)]
struct Stopped {
    error: Error,
    observed_at: Clock,
}

struct Shared {
    retired_through: AtomicU64,
    submitted_through: AtomicU64,
    wake: Notify,
    stop: watch::Sender<Option<Stopped>>,
    resumption: Mutex<Option<ResumptionPoint>>,
    // An invalidation can arrive on a resumed connection before it issues a
    // local handle. Retain that fact for the supervisor's inherited point.
    resumption_invalidated: AtomicBool,
}

/// The latest point from which the service said this session can be resumed.
#[derive(Clone, Debug)]
pub struct ResumptionPoint {
    handle: ResumptionHandle,
    current: bool,
    advertised_session: SessionId,
}
impl ResumptionPoint {
    pub fn handle(&self) -> &ResumptionHandle {
        &self.handle
    }
    /// Whether the latest applicable service metadata advertised this point
    /// as current. This does not acknowledge which client messages the point
    /// includes or guarantee the model's exact restored context.
    pub fn is_current(&self) -> bool {
        self.current
    }
    pub(crate) fn advertised_session(&self) -> SessionId {
        self.advertised_session
    }
    pub(crate) fn invalidate(&mut self) {
        self.current = false;
    }
}
impl Shared {
    /// The first failure is kept. A local close always takes precedence, so a
    /// privacy stop still drops output queued behind an earlier remote failure.
    fn fail(&self, error: Error) {
        self.stop.send_if_modified(|value| {
            if value.is_none()
                || (error == Error::Closed && value.is_some_and(|stop| stop.error != Error::Closed))
            {
                *value = Some(Stopped {
                    error,
                    observed_at: Clock::now(),
                });
                true
            } else {
                false
            }
        });
        self.wake.notify_one();
    }
}

#[derive(Clone)]
pub struct InputSender {
    commands: mpsc::Sender<Command>,
    shared: Arc<Shared>,
    state: watch::Receiver<State>,
}
impl InputSender {
    /// Queue a new admitted request. This synchronously retires earlier output
    /// from this handle; root must ALSO revoke/flush already delivered playback.
    pub fn try_start(&self, request: RequestId) -> Result<()> {
        self.ensure_ready()?;
        if request.get() <= self.shared.retired_through.load(Ordering::Acquire)
            || self
                .shared
                .submitted_through
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |old| {
                    (request.get() > old).then_some(request.get())
                })
                .is_err()
        {
            return Err(Error::StaleRequest);
        }
        self.shared
            .retired_through
            .fetch_max(request.get() - 1, Ordering::AcqRel);
        self.shared.wake.notify_one();
        self.enqueue(Command::Start(request))
    }
    /// `captured_at` is acquisition time in this process's Instant domain, not
    /// queue receipt time. A process wrapper must preserve IPC acquisition age
    /// when translating its shared OS monotonic timestamps into this domain.
    pub fn try_audio(
        &self,
        request: RequestId,
        sequence: u64,
        captured_at: Instant,
        samples: &[i16],
    ) -> Result<()> {
        self.ensure_ready()?;
        if request.get() <= self.shared.retired_through.load(Ordering::Acquire) {
            return Err(Error::StaleRequest);
        }
        check_age(captured_at)?;
        if samples.is_empty() || samples.len() > MAX_INPUT_SAMPLES {
            return Err(Error::InvalidAudio);
        }
        self.enqueue(Command::Audio(Audio {
            request,
            sequence,
            captured_at,
            samples: samples.to_vec(),
        }))
    }
    pub fn try_end(&self, request: RequestId) -> Result<()> {
        self.enqueue(Command::End(request))
    }
    /// Permanent local retirement is synchronous and cannot be blocked by a full
    /// audio queue. Network interruption follows asynchronously. This does not
    /// revoke permits or flush samples already handed to an external speaker.
    pub fn retire(&self, request: RequestId) {
        self.shared
            .retired_through
            .fetch_max(request.get(), Ordering::AcqRel);
        self.shared.wake.notify_one();
    }
    pub fn shutdown(&self) {
        self.shared.fail(Error::Closed);
    }
    fn ensure_ready(&self) -> Result<()> {
        if self.shared.stop.borrow().is_some() || *self.state.borrow() != State::Ready {
            return Err(Error::Closed);
        }
        Ok(())
    }
    fn enqueue(&self, command: Command) -> Result<()> {
        self.ensure_ready()?;
        self.commands
            .try_send(command)
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => {
                    self.shared.fail(Error::Backpressure);
                    Error::Backpressure
                }
                mpsc::error::TrySendError::Closed(_) => Error::Closed,
            })
    }
}

pub struct Connection {
    input: InputSender,
    events: mpsc::Receiver<Event>,
    state: watch::Receiver<State>,
    task: JoinHandle<()>,
    ready_at: Clock,
}
impl Connection {
    pub fn input(&self) -> InputSender {
        self.input.clone()
    }
    pub fn state(&self) -> State {
        self.observed_state().0
    }
    /// Source-local observation time, retained even when the supervisor is
    /// backpressured. This is not when a remote network failure occurred.
    pub(crate) fn observed_state(&self) -> (State, Clock) {
        match *self.input.shared.stop.borrow() {
            Some(stop) => (
                if stop.error == Error::Closed {
                    State::Closed
                } else {
                    State::Disconnected(stop.error)
                },
                stop.observed_at,
            ),
            None => (*self.state.borrow(), self.ready_at),
        }
    }
    pub(crate) fn ready_at(&self) -> Clock {
        self.ready_at
    }
    pub fn subscribe_state(&self) -> watch::Receiver<State> {
        self.state.clone()
    }
    pub fn shutdown(&self) {
        self.input.shutdown();
    }
    /// Latest retained resumption point, if the configuration allows retention
    /// and the service has issued one. Remains readable after the connection ends.
    pub fn resumption(&self) -> Option<ResumptionPoint> {
        self.input
            .shared
            .resumption
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
    pub(crate) fn resumption_invalidated(&self) -> bool {
        self.input
            .shared
            .resumption_invalidated
            .load(Ordering::Acquire)
    }
    /// Cancellation-safe receiver. Already queued retired audio/text is dropped,
    /// while lifecycle events retain their original lineage for bookkeeping.
    /// Output received before a remote or transport failure is still delivered,
    /// in order, ahead of the end of the stream: a failure must not truncate an
    /// answer that had already arrived. [`Connection::state`] reports the
    /// failure, and input is refused, as soon as it happens; the stream ends
    /// only after that buffered output has been taken. A local shutdown drops
    /// queued output.
    pub async fn next_event(&mut self) -> Option<Event> {
        while let Some(event) = self.events.recv().await {
            let output_request = match &event {
                Event::Audio { lineage, .. }
                | Event::OutputTranscript { lineage, .. }
                | Event::ModelText { lineage, .. } => Some(lineage.request.get()),
                _ => None,
            };
            if output_request.is_some_and(|id| {
                self.input
                    .shared
                    .stop
                    .borrow()
                    .is_some_and(|stop| stop.error == Error::Closed)
                    || id <= self.input.shared.retired_through.load(Ordering::Acquire)
            }) {
                continue;
            }
            return Some(event);
        }
        None
    }
}
impl Drop for Connection {
    fn drop(&mut self) {
        self.input.shutdown();
        self.task.abort();
    }
}

/// Establish TLS, send setup, and wait for setupComplete before returning Ready.
/// No implicit reconnect, resumption, tools, or hidden agent task.
pub async fn connect(config: SessionConfig, session: SessionId) -> Result<Connection> {
    config.timeouts.validate()?;
    let request = config.request()?;
    let (socket, _) = timeout(
        config.timeouts.connect,
        connect_async_with_config(request, Some(socket_config()), true),
    )
    .await
    .map_err(|_| Error::ConnectTimeout)?
    .map_err(transport_error)?;
    setup_and_spawn(socket, config, session).await
}

pub(crate) fn socket_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .read_buffer_size(16_384)
        .write_buffer_size(0)
        .max_write_buffer_size(MAX_WIRE_BYTES + 4096)
        .max_message_size(Some(MAX_WIRE_BYTES))
        .max_frame_size(Some(MAX_WIRE_BYTES))
}

pub(crate) async fn setup_and_spawn<S>(
    mut socket: WebSocketStream<S>,
    config: SessionConfig,
    session: SessionId,
) -> Result<Connection>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    send(
        &mut socket,
        Message::text(wire::encode_setup(&config)?),
        config.timeouts.write,
    )
    .await?;
    let setup = async {
        loop {
            let message = socket
                .next()
                .await
                .ok_or(Error::PeerClosed { code: None })?
                .map_err(transport_error)?;
            match message {
                Message::Text(text) => {
                    let frame = wire::decode_server(text.as_bytes())?;
                    if frame.setup_complete {
                        return Ok(Clock::now());
                    }
                    if frame.content.is_some()
                        || frame.go_away.is_some()
                        || frame.voice_activity.is_some()
                    {
                        return Err(Error::UnexpectedResponse);
                    }
                }
                Message::Binary(bytes) => {
                    let frame = wire::decode_server(&bytes)?;
                    if frame.setup_complete {
                        return Ok(Clock::now());
                    }
                    if frame.content.is_some()
                        || frame.go_away.is_some()
                        || frame.voice_activity.is_some()
                    {
                        return Err(Error::UnexpectedResponse);
                    }
                }
                Message::Ping(bytes) => {
                    send(&mut socket, Message::Pong(bytes), config.timeouts.write).await?
                }
                Message::Pong(_) => {}
                Message::Close(frame) => return Err(closed(frame)),
                Message::Frame(_) => return Err(Error::MalformedMessage),
            }
        }
    };
    let ready_at = timeout(config.timeouts.setup, setup)
        .await
        .map_err(|_| Error::SetupTimeout)??;
    let (commands_tx, commands) = mpsc::channel(INPUT_QUEUE_CAPACITY);
    let (events_tx, events) = mpsc::channel(EVENT_QUEUE_CAPACITY);
    let (state_tx, state) = watch::channel(State::Ready);
    let (stop, stop_rx) = watch::channel(None);
    let shared = Arc::new(Shared {
        retired_through: AtomicU64::new(0),
        submitted_through: AtomicU64::new(0),
        wake: Notify::new(),
        stop,
        resumption: Mutex::new(None),
        resumption_invalidated: AtomicBool::new(false),
    });
    let input = InputSender {
        commands: commands_tx,
        shared: shared.clone(),
        state: state.clone(),
    };
    let actor = Actor {
        socket,
        session,
        timeouts: config.timeouts,
        retain: config.resumption != Resumption::Off,
        policy: config.barrier,
        output_buffer: config.output_buffer,
        commands,
        events: events_tx,
        state: state_tx,
        outbox: VecDeque::new(),
        outbox_bytes: 0,
        waiting_since: None,
        gated: false,
        purged_through: 0,
        shared,
        stop: stop_rx,
        current: None,
        barrier: None,
        last_responder: None,
        stale_terminal: None,
        go_away: false,
        activity_open: false,
        last_request: 0,
        read_at: Clock::now(),
        ping_at: Clock::now(),
    };
    // The actor publishes its terminal state before it lets go of the event
    // sender, so a receiver that sees the end of the stream never reads Ready
    // and loses the sanitized failure, even on a multithreaded runtime.
    let task = tokio::spawn(actor.run());
    Ok(Connection {
        input,
        events,
        state,
        task,
        ready_at,
    })
}

struct Audio {
    request: RequestId,
    sequence: u64,
    captured_at: Instant,
    samples: Vec<i16>,
}
enum Command {
    Start(RequestId),
    Audio(Audio),
    End(RequestId),
}
struct InputState {
    open: bool,
    last_sequence: Option<u64>,
    last_capture: Option<Instant>,
}
impl InputState {
    fn new() -> Self {
        Self {
            open: true,
            last_sequence: None,
            last_capture: None,
        }
    }
    fn accept(&mut self, audio: &Audio) -> Result<()> {
        check_age(audio.captured_at)?;
        if !self.open
            || self
                .last_sequence
                .is_some_and(|last| last.checked_add(1) != Some(audio.sequence))
            || self
                .last_capture
                .is_some_and(|last| audio.captured_at < last)
        {
            return Err(Error::InputSequence);
        }
        self.last_sequence = Some(audio.sequence);
        self.last_capture = Some(audio.captured_at);
        Ok(())
    }
}
struct Turn {
    lineage: Lineage,
    input: InputState,
    /// Cancelled locally while its input was still open. Its End drops it.
    retired: bool,
    /// The person finished, but activityEnd is withheld until an earlier
    /// response has settled.
    end_held: bool,
    /// activityEnd sent: the service now owes a response or a terminal event.
    /// Until then nothing the service sends can belong to this request.
    ended_at: Option<Clock>,
    first_output_at: Option<Clock>,
    progress_at: Option<Clock>,
    generation_at: Option<Clock>,
    output_samples: u64,
    audio_sequence: u64,
}
impl Turn {
    fn new(lineage: Lineage) -> Self {
        Self {
            lineage,
            input: InputState::new(),
            retired: false,
            end_held: false,
            ended_at: None,
            first_output_at: None,
            progress_at: None,
            generation_at: None,
            output_samples: 0,
            audio_sequence: 0,
        }
    }
}
/// An earlier response that must settle before the newest request may be
/// committed with activityEnd. The newest request's audio is never held: the
/// service cannot answer an activity before its End, so everything that
/// arrives while a barrier stands belongs to the earlier response.
struct Barrier {
    /// Unknown when stray output arrives with no response on record.
    old: Option<Lineage>,
    /// When the earlier response was asked to stop, or first seen.
    since: Clock,
    /// That moment, then the latest event of the earlier response.
    last_event_at: Clock,
}
impl Barrier {
    fn new(old: Option<Lineage>) -> Self {
        let now = Clock::now();
        Self {
            old,
            since: now,
            last_event_at: now,
        }
    }
}
struct Actor<S> {
    socket: WebSocketStream<S>,
    session: SessionId,
    timeouts: Timeouts,
    retain: bool,
    policy: BarrierPolicy,
    output_buffer: usize,
    commands: mpsc::Receiver<Command>,
    events: mpsc::Sender<Event>,
    state: watch::Sender<State>,
    /// Decoded events the bounded event queue has not accepted yet. Bounded by
    /// `output_buffer` bytes: beyond that the socket is not read.
    outbox: VecDeque<Event>,
    outbox_bytes: usize,
    /// Since when the consumer has accepted nothing while events waited.
    waiting_since: Option<Clock>,
    gated: bool,
    purged_through: u64,
    shared: Arc<Shared>,
    stop: watch::Receiver<Option<Stopped>>,
    /// The newest request: the only one that accepts input.
    current: Option<Turn>,
    barrier: Option<Barrier>,
    /// Most recent request that produced output: the only possible owner of an
    /// output transcript that trails its audio.
    last_responder: Option<Lineage>,
    /// When a stray `interrupted` was dropped whose companion completion has
    /// not been seen yet. Only honored within `Timeouts::barrier` of it.
    stale_terminal: Option<Clock>,
    go_away: bool,
    activity_open: bool,
    last_request: u64,
    read_at: Clock,
    ping_at: Clock,
}
fn cost(event: &Event) -> usize {
    64 + match event {
        Event::Audio { pcm, .. } => pcm.len() * 2,
        Event::OutputTranscript { text, .. }
        | Event::ModelText { text, .. }
        | Event::UncorrelatedInputTranscript { text, .. } => text.len(),
        _ => 0,
    }
}
impl<S: AsyncRead + AsyncWrite + Unpin> Actor<S> {
    async fn run(mut self) {
        let result = self.serve().await;
        let terminal = match result {
            Ok(()) | Err(Error::Closed) => {
                self.shared.fail(Error::Closed);
                State::Closed
            }
            Err(error) => {
                // Refuse input and report the failure now. Output that had
                // already arrived is still handed over, in order, below.
                self.shared.fail(error);
                State::Disconnected(error)
            }
        };
        self.state.send_replace(terminal);
        // A consumer that has stopped taking events is the failure itself;
        // every other failure leaves earlier, valid output worth delivering.
        if !matches!(result, Ok(()) | Err(Error::Closed | Error::Backpressure)) {
            self.drain().await;
        }
    }
    /// After a failure: keep handing buffered output to a consumer that keeps
    /// taking it, for as long as it does. Nothing is read or sent any more.
    async fn drain(&mut self) {
        let mut tick = tokio::time::interval(Duration::from_millis(10));
        tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            if self
                .stop
                .borrow()
                .is_some_and(|stop| stop.error == Error::Closed)
            {
                return;
            }
            self.purge_retired();
            self.flush();
            if self.outbox.is_empty() || self.delivery_expired() {
                return;
            }
            tokio::select! {
                _ = capacity(&self.events) => {},
                _ = self.shared.wake.notified() => {},
                _ = tick.tick() => {},
            }
        }
    }
    async fn serve(&mut self) -> Result<()> {
        let mut tick = tokio::time::interval(Duration::from_millis(10));
        tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            if let Some(stop) = *self.stop.borrow() {
                return Err(stop.error);
            }
            self.retire_active().await?;
            self.purge_retired();
            self.enforce_barrier().await?;
            self.flush();
            self.check_deadlines()?;
            if self.go_away
                && self.current.is_none()
                && self.barrier.is_none()
                && self.outbox.is_empty()
            {
                // Nothing is owed on this connection: end it at a clean
                // boundary instead of waiting for the announced close.
                return Err(Error::ServerGoAway);
            }
            if self.ping_at.elapsed() >= self.timeouts.keepalive {
                send(
                    &mut self.socket,
                    Message::Ping(Vec::new().into()),
                    self.timeouts.write,
                )
                .await?;
                self.ping_at = Clock::now();
            }
            // Output is buffered up to a fixed amount of audio so the answer is
            // received at network speed whatever the playback speed. Only past
            // that bound does the socket go unread and transport flow control
            // hold the service back. Commands, local retirement and shutdown
            // stay live throughout.
            let waiting = !self.outbox.is_empty();
            let gated = self.gated;
            tokio::select! {
                _ = self.shared.wake.notified() => {},
                _ = tick.tick() => {},
                // This actor is the only sender, so the released slot is still
                // free when the next iteration flushes the outbox into it.
                ready = capacity(&self.events), if waiting => ready?,
                command = self.commands.recv() => match command {
                    Some(command) => self.command(command).await?,
                    None => return Ok(()),
                },
                incoming = self.socket.next(), if !gated => {
                    let incoming = incoming
                        .ok_or(Error::PeerClosed { code: None })?
                        .map_err(transport_error)?;
                    self.read_at = Clock::now();
                    self.message(incoming).await?;
                    // One server message per scheduling turn: a burst already in
                    // the socket buffer cannot starve the caller's control tick.
                    tokio::task::yield_now().await;
                }
            }
        }
    }
    fn delivery_expired(&self) -> bool {
        self.waiting_since
            .is_some_and(|since| since.elapsed() >= self.timeouts.deliver)
    }
    fn check_deadlines(&self) -> Result<()> {
        if self.delivery_expired() {
            return Err(Error::Backpressure);
        }
        if self.gated {
            // The socket is deliberately unread, so neither liveness nor
            // response progress can be judged until reading resumes.
            return Ok(());
        }
        if self.read_at.elapsed() >= self.timeouts.read {
            return Err(Error::ReadTimeout);
        }
        let Some(turn) = self.current.as_ref() else {
            return Ok(());
        };
        let Some(ended) = turn.ended_at else {
            return Ok(());
        };
        if ended.elapsed() >= self.timeouts.turn {
            return Err(Error::ResponseTimeout);
        }
        match (turn.first_output_at, turn.generation_at) {
            (None, _) => {
                if ended.elapsed() >= self.timeouts.response {
                    return Err(Error::ResponseTimeout);
                }
            }
            (Some(_), None) => {
                if turn
                    .progress_at
                    .is_some_and(|at| at.elapsed() >= self.timeouts.stall)
                {
                    return Err(Error::StalledResponse);
                }
            }
            (Some(first), Some(generated)) => {
                // The service withholds the idle completion while it assumes the
                // generated audio is still playing in real time.
                let playback = Duration::from_micros(
                    turn.output_samples.saturating_mul(1_000_000) / u64::from(OUTPUT_RATE),
                );
                let due = (first + playback).max(generated) + self.timeouts.completion;
                if Clock::now() >= due {
                    return Err(Error::CompletionTimeout);
                }
            }
        }
        Ok(())
    }
    async fn start_activity(&mut self) -> Result<()> {
        if !self.activity_open {
            send(
                &mut self.socket,
                Message::text(wire::ACTIVITY_START),
                self.timeouts.write,
            )
            .await?;
            self.activity_open = true;
        }
        Ok(())
    }
    /// Commit the current request: from here on the service owes it a response.
    async fn commit(&mut self) -> Result<()> {
        send(
            &mut self.socket,
            Message::text(wire::ACTIVITY_END),
            self.timeouts.write,
        )
        .await?;
        self.activity_open = false;
        if let Some(turn) = self.current.as_mut() {
            turn.end_held = false;
            turn.ended_at = Some(Clock::now());
        }
        Ok(())
    }
    fn retired_through(&self) -> u64 {
        self.shared.retired_through.load(Ordering::Acquire)
    }
    async fn retire_active(&mut self) -> Result<()> {
        let retired_through = self.retired_through();
        let Some(turn) = self.current.as_mut() else {
            return Ok(());
        };
        if turn.lineage.request.get() > retired_through {
            return Ok(());
        }
        if turn.ended_at.is_some() {
            // Committed: its response becomes the earlier stream that must
            // settle. Explicit activityStart is the only interruption request
            // available with provider activity detection disabled.
            self.barrier = Some(Barrier::new(Some(turn.lineage)));
            self.current = None;
            return self.start_activity().await;
        }
        if turn.input.open {
            // No activityEnd was sent, so the service owes this request
            // nothing. Its End drops it without asking for a response.
            turn.retired = true;
        } else {
            // Its End was withheld and now never needs sending.
            self.current = None;
        }
        Ok(())
    }
    async fn enforce_barrier(&mut self) -> Result<()> {
        let Some(barrier) = self.barrier.as_ref() else {
            return Ok(());
        };
        if barrier.since.elapsed() >= self.timeouts.response {
            // Still producing this long after being asked to stop. Waiting on
            // is not an option under either policy: the next request would
            // never be committed.
            return Err(Error::BarrierTimeout);
        }
        if barrier.last_event_at.elapsed() < self.timeouts.barrier {
            return Ok(());
        }
        match self.policy {
            BarrierPolicy::Require => Err(Error::BarrierTimeout),
            BarrierPolicy::AssumeAfterQuiet => {
                // Asked to stop and silent for the whole bound. The service
                // never confirmed it, so whatever follows is flagged.
                let superseded = barrier.old.map(|lineage| lineage.request);
                let successor = self.current.as_ref().map(|turn| turn.lineage.request);
                self.barrier = None;
                self.emit(Event::BarrierAssumed {
                    session: self.session,
                    superseded,
                    successor,
                });
                self.release().await
            }
        }
    }
    /// The earlier response has settled: a withheld End may go out now.
    async fn release(&mut self) -> Result<()> {
        if self.current.as_ref().is_some_and(|turn| turn.end_held) {
            self.commit().await?;
        }
        Ok(())
    }
    async fn command(&mut self, command: Command) -> Result<()> {
        match command {
            Command::Start(request) => {
                if request.get() <= self.last_request
                    || self.current.as_ref().is_some_and(|turn| turn.input.open)
                {
                    return Err(Error::OverlappingInput);
                }
                self.last_request = request.get();
                let lineage = Lineage {
                    session: self.session,
                    request,
                };
                // try_start retired every earlier request. A committed one
                // becomes the barrier; one whose End was withheld was never
                // committed, and its audio stays in the activity this request
                // continues. It cannot receive a response of its own.
                self.retire_active().await?;
                self.current = Some(Turn::new(lineage));
                self.start_activity().await?;
                self.emit(Event::InputStarted {
                    lineage,
                    waiting_for_barrier: self.barrier.is_some(),
                });
            }
            Command::Audio(audio) => {
                if audio.request.get() <= self.retired_through() {
                    return Ok(());
                }
                let turn = self
                    .current
                    .as_mut()
                    .filter(|turn| turn.lineage.request == audio.request)
                    .ok_or(Error::StaleRequest)?;
                turn.input.accept(&audio)?;
                send_audio(&mut self.socket, &audio, self.timeouts.write).await?;
            }
            Command::End(request) => {
                let retired_through = self.retired_through();
                let turn = self
                    .current
                    .as_mut()
                    .filter(|turn| turn.lineage.request == request && turn.input.open)
                    .ok_or(Error::StaleRequest)?;
                turn.input.open = false;
                if turn.retired || request.get() <= retired_through {
                    // Cancelled before commit. Sending activityEnd would ask
                    // for a response nobody owns; leave the activity open for
                    // the next admitted request to continue.
                    self.current = None;
                } else if self.barrier.is_some() {
                    // Withheld until the earlier response settles, so output
                    // after this End can only belong to this request.
                    turn.end_held = true;
                } else {
                    self.commit().await?;
                }
            }
        }
        Ok(())
    }
    fn decode(&self, bytes: &[u8]) -> Result<wire::ServerFrame> {
        if self.retain {
            wire::decode_server_retaining(bytes)
        } else {
            wire::decode_server(bytes)
        }
    }
    async fn message(&mut self, message: Message) -> Result<()> {
        let frame = match message {
            Message::Text(text) => self.decode(text.as_bytes())?,
            Message::Binary(bytes) => self.decode(&bytes)?,
            Message::Ping(bytes) => {
                send(&mut self.socket, Message::Pong(bytes), self.timeouts.write).await?;
                return Ok(());
            }
            Message::Pong(_) => return Ok(()),
            Message::Close(frame) => return Err(closed(frame)),
            Message::Frame(_) => return Err(Error::MalformedMessage),
        };
        if frame.setup_complete {
            return Err(Error::UnexpectedResponse);
        }
        if let Some(update) = frame.resumption {
            self.shared
                .resumption_invalidated
                .store(update.handle.is_none(), Ordering::Release);
            let mut point = self
                .shared
                .resumption
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            match update.handle {
                Some(handle) => {
                    *point = Some(ResumptionPoint {
                        handle,
                        current: true,
                        advertised_session: self.session,
                    });
                }
                // The previous handle still resumes, but only up to its own point.
                None => {
                    if let Some(point) = point.as_mut() {
                        point.current = false;
                    }
                }
            }
        }
        if let Some(notice) = frame.go_away
            && !self.go_away
        {
            self.go_away = true;
            self.emit(Event::GoAway {
                session: self.session,
                time_left: notice.time_left,
            });
        }
        if let Some(activity) = frame.voice_activity {
            self.emit(Event::VoiceActivity {
                session: self.session,
                kind: activity.kind,
                audio_offset: activity.audio_offset,
            });
        }
        if let Some(content) = frame.content {
            self.content(content).await?;
        }
        Ok(())
    }
    fn discard(&mut self, reason: Discard) {
        self.emit(Event::Discarded {
            session: self.session,
            reason,
        });
    }
    /// The service gives output transcripts no ordering relative to audio or
    /// completion. One that arrives before the current request has produced
    /// output cannot describe it, so it stays with the previous responder.
    fn output_transcript(&mut self, transcript: wire::Transcript) {
        let owner = match &self.current {
            Some(turn)
                if turn.ended_at.is_some()
                    && (turn.first_output_at.is_some() || self.last_responder.is_none()) =>
            {
                Some(turn.lineage)
            }
            _ => self.last_responder,
        };
        match owner {
            Some(lineage) => self.emit(Event::OutputTranscript {
                lineage,
                text: transcript.text,
                finished: transcript.finished,
            }),
            None => self.discard(Discard::UnownedOutput),
        }
    }
    async fn content(&mut self, mut content: wire::ServerContent) -> Result<()> {
        if let Some(transcript) = content.input_transcript.take() {
            self.emit(Event::UncorrelatedInputTranscript {
                session: self.session,
                text: transcript.text,
                finished: transcript.finished,
            });
        }
        let transcript = content.output_transcript.take();
        let payload = !content.audio.is_empty() || !content.text.is_empty();
        let terminal = content.interrupted || content.turn_complete;
        if payload || terminal || content.generation_complete {
            // Apply any local retirement first so ownership below is current.
            self.retire_active().await?;
            if self
                .current
                .as_ref()
                .is_some_and(|turn| turn.ended_at.is_some())
            {
                self.response(content, payload);
            } else {
                self.earlier(content, payload).await?;
            }
        }
        if let Some(transcript) = transcript {
            self.output_transcript(transcript);
        }
        Ok(())
    }
    /// No request is committed, so the service cannot be answering one.
    /// Everything here belongs to an earlier response.
    async fn earlier(&mut self, content: wire::ServerContent, payload: bool) -> Result<()> {
        let now = Clock::now();
        let producing = payload || content.generation_complete;
        let settled = content.turn_complete && content.idle;
        let known = self.barrier.as_ref().and_then(|barrier| barrier.old);
        if let Some(lineage) = known {
            // A response this client retired. Its output is dropped here; its
            // lifecycle is still reported under its own name.
            if producing {
                self.last_responder = Some(lineage);
            }
            if content.interrupted {
                self.emit(Event::Interrupted { lineage });
            }
            if content.turn_complete {
                self.emit(Event::TurnComplete {
                    lineage,
                    idle: content.idle,
                });
            }
        } else {
            if content.interrupted && !content.turn_complete {
                self.stale_terminal = Some(now);
            }
            self.discard(if content.interrupted {
                Discard::LateInterruption
            } else if self.current.is_none() {
                Discard::UnownedOutput
            } else if producing {
                Discard::LateOutput
            } else {
                Discard::LateTerminal
            });
        }
        if settled {
            if self.barrier.take().is_some() {
                self.release().await?;
            }
        } else if let Some(barrier) = self.barrier.as_mut() {
            barrier.last_event_at = now;
        } else if producing {
            // A response is still producing after its barrier, or with none
            // on record. Hold the next commit until it settles, exactly as
            // for a response this client retired itself.
            self.barrier = Some(Barrier::new(None));
        }
        Ok(())
    }
    /// The current request is committed and no earlier response is pending.
    fn response(&mut self, content: wire::ServerContent, payload: bool) {
        let barrier = self.timeouts.barrier;
        let Some(turn) = self.current.as_mut() else {
            return;
        };
        let lineage = turn.lineage;
        let now = Clock::now();
        if content.interrupted {
            // This client has not asked to interrupt this request, and nothing
            // else can with provider activity detection disabled. The event is
            // the delayed result of an earlier activityStart; it must neither
            // cancel this request nor confirm that anyone spoke.
            if !content.turn_complete {
                self.stale_terminal = Some(now);
            }
            return self.discard(Discard::LateInterruption);
        }
        if payload || content.generation_complete {
            self.stale_terminal = None;
            turn.first_output_at.get_or_insert(now);
            turn.progress_at = Some(now);
            self.last_responder = Some(lineage);
        }
        let sequence = turn.audio_sequence;
        if !content.audio.is_empty() {
            turn.audio_sequence = sequence.saturating_add(1);
            turn.output_samples = turn
                .output_samples
                .saturating_add(content.audio.len() as u64);
            // Audio after a completed generation reopens it.
            turn.generation_at = None;
        }
        if content.generation_complete {
            turn.generation_at = Some(now);
        }
        let answered = turn.first_output_at.is_some();
        if !content.audio.is_empty() {
            self.emit(Event::Audio {
                lineage,
                sequence,
                pcm: content.audio,
            });
        }
        for text in content.text {
            self.emit(Event::ModelText { lineage, text });
        }
        if content.generation_complete {
            self.emit(Event::GenerationComplete { lineage });
        }
        if content.turn_complete {
            let companion = self
                .stale_terminal
                .take()
                .is_some_and(|at| at.elapsed() < barrier);
            if companion && !answered {
                // Companion of the stray interruption dropped just before.
                return self.discard(Discard::LateTerminal);
            }
            self.emit(Event::TurnComplete {
                lineage,
                idle: content.idle,
            });
            if content.idle {
                self.current = None;
            } else if let Some(turn) = self.current.as_mut() {
                turn.progress_at = Some(now);
            }
        }
    }
    fn emit(&mut self, event: Event) {
        self.outbox_bytes += cost(&event);
        self.outbox.push_back(event);
        self.flush();
    }
    /// Retired output still waiting here would only be skipped by the receiver
    /// later. Drop it now so a cancelled long answer frees its buffer at once.
    fn purge_retired(&mut self) {
        let retired_through = self.retired_through();
        if retired_through == self.purged_through {
            return;
        }
        self.purged_through = retired_through;
        self.outbox.retain(|event| match event {
            Event::Audio { lineage, .. }
            | Event::OutputTranscript { lineage, .. }
            | Event::ModelText { lineage, .. } => lineage.request.get() > retired_through,
            _ => true,
        });
        self.outbox_bytes = self.outbox.iter().map(cost).sum();
    }
    fn flush(&mut self) {
        let mut accepted = false;
        while let Some(event) = self.outbox.pop_front() {
            let bytes = cost(&event);
            match self.events.try_send(event) {
                Ok(()) => {
                    accepted = true;
                    self.outbox_bytes = self.outbox_bytes.saturating_sub(bytes);
                }
                Err(mpsc::error::TrySendError::Full(event)) => {
                    self.outbox.push_front(event);
                    break;
                }
                Err(mpsc::error::TrySendError::Closed(_)) => {
                    self.outbox.clear();
                    self.outbox_bytes = 0;
                    break;
                }
            }
        }
        // The delivery bound measures a consumer taking nothing at all, not
        // one that is slower than the network, as real-time playback always is.
        if self.outbox.is_empty() {
            self.waiting_since = None;
        } else if accepted || self.waiting_since.is_none() {
            self.waiting_since = Some(Clock::now());
        }
        let gated = self.outbox_bytes >= self.output_buffer;
        if self.gated && !gated {
            // Time spent not reading is this client's doing, not a stall.
            let now = Clock::now();
            self.read_at = now;
            if let Some(progress) = self
                .current
                .as_mut()
                .and_then(|turn| turn.progress_at.as_mut())
            {
                *progress = now;
            }
        }
        self.gated = gated;
    }
}

async fn capacity(events: &mpsc::Sender<Event>) -> Result<()> {
    events.reserve().await.map(drop).map_err(|_| Error::Closed)
}

/// Quota and usage refusals are recognised from the close reason so they are
/// not retried as ordinary disconnects. The reason selects a fixed category
/// and is dropped here; it is never retained, logged or returned.
fn closed(frame: Option<tungstenite::protocol::CloseFrame>) -> Error {
    let Some(frame) = frame else {
        return Error::PeerClosed { code: None };
    };
    let reason = frame.reason.to_ascii_lowercase();
    if [
        "quota",
        "resource_exhausted",
        "resource exhausted",
        "usage limit",
        "rate limit",
    ]
    .iter()
    .any(|marker| reason.contains(marker))
    {
        return Error::QuotaExceeded;
    }
    Error::PeerClosed {
        code: Some(frame.code.into()),
    }
}

fn check_age(captured_at: Instant) -> Result<Duration> {
    Instant::now()
        .checked_duration_since(captured_at)
        .filter(|age| *age < MAX_INPUT_AGE)
        .ok_or(Error::StaleInput)
}
async fn send_audio<S: AsyncRead + AsyncWrite + Unpin>(
    socket: &mut WebSocketStream<S>,
    audio: &Audio,
    deadline: Duration,
) -> Result<()> {
    let age = check_age(audio.captured_at)?;
    let deadline = deadline.min(MAX_INPUT_AGE - age);
    send(
        socket,
        Message::text(wire::encode_audio(&audio.samples)?),
        deadline,
    )
    .await
}
async fn send<S: AsyncRead + AsyncWrite + Unpin>(
    socket: &mut WebSocketStream<S>,
    message: Message,
    deadline: Duration,
) -> Result<()> {
    timeout(deadline, socket.send(message))
        .await
        .map_err(|_| Error::WriteTimeout)?
        .map_err(transport_error)
}
fn transport_error(error: tungstenite::Error) -> Error {
    match error {
        tungstenite::Error::Http(response) => match response.status().as_u16() {
            401 | 403 => Error::Authentication,
            429 => Error::RateLimited,
            500..=599 => Error::ServerUnavailable,
            _ => Error::Transport,
        },
        tungstenite::Error::Capacity(_) => Error::MessageTooLarge,
        tungstenite::Error::WriteBufferFull(_) => Error::Backpressure,
        tungstenite::Error::ConnectionClosed | tungstenite::Error::AlreadyClosed => {
            Error::PeerClosed { code: None }
        }
        _ => Error::Transport,
    }
}

#[cfg(test)]
mod tests;
