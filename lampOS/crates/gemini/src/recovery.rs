//! Bounded reconnection around one live [`Connection`] at a time.
//!
//! The supervisor never replays input or re-requests an answer. A failed
//! connection is reported at once and replaced within the caller's policy,
//! while output it had already received keeps being delivered in order, at
//! whatever speed the consumer plays it. Only when that output is exhausted
//! is the accepted request reported as settled or lost. Conversation context
//! survives only through a server-issued resumption handle; a reconnect
//! without one is reported as fresh, never presented as continuity.
use crate::{
    Connection, Error, Event, InputSender, Lineage, Recovery, RequestId, Result, ResumptionHandle,
    ResumptionPoint, SessionConfig, SessionId, State,
};
use futures_util::FutureExt;
use std::{cell::Cell, collections::VecDeque, fmt, pin::Pin, time::Duration};
// The runtime clock, so scripted tests can cover long outages exactly.
use tokio::{sync::watch, task::JoinHandle, time::Instant};

pub type Attempt = Pin<Box<dyn Future<Output = Result<Connection>> + Send>>;

/// Opens one provider connection. Every call is a new session.
pub trait Connect {
    fn connect(&mut self, session: SessionId, resume: Option<ResumptionHandle>) -> Attempt;
}

/// Production connector: TLS WebSocket to the configured endpoint.
pub struct Dialer {
    config: SessionConfig,
}
impl Dialer {
    pub fn new(config: SessionConfig) -> Self {
        Self { config }
    }
}
impl Connect for Dialer {
    fn connect(&mut self, session: SessionId, resume: Option<ResumptionHandle>) -> Attempt {
        Box::pin(crate::connect(self.config.clone().resume(resume), session))
    }
}

/// Limits on reconnect work. The first connection is never retried.
#[derive(Clone, Copy, Debug)]
pub struct RecoveryPolicy {
    /// Connection attempts per outage. Zero disables recovery.
    pub max_attempts: u32,
    /// Wait before the second attempt; doubles up to `max_backoff`.
    pub first_backoff: Duration,
    pub max_backoff: Duration,
    /// Connection loss to completed setup, across all attempts.
    pub outage_budget: Duration,
    /// Recoveries allowed within `window` before the link is declared unstable.
    pub max_recoveries: u32,
    pub window: Duration,
    /// Refuse to reconnect when no resumption handle is available, instead of
    /// continuing with a session that has forgotten the conversation.
    pub require_context: bool,
}
impl Default for RecoveryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 4,
            first_backoff: Duration::from_millis(500),
            max_backoff: Duration::from_secs(4),
            outage_budget: Duration::from_secs(15),
            max_recoveries: 3,
            window: Duration::from_secs(60),
            require_context: true,
        }
    }
}
impl RecoveryPolicy {
    /// Any connection loss ends the supervisor.
    pub const DISABLED: Self = Self {
        max_attempts: 0,
        first_backoff: Duration::ZERO,
        max_backoff: Duration::ZERO,
        outage_budget: Duration::ZERO,
        max_recoveries: 0,
        window: Duration::ZERO,
        require_context: true,
    };
    fn validate(self) -> Result<()> {
        if self.max_attempts > 16
            || self.max_recoveries > 64
            || self.first_backoff > self.max_backoff
            || self.max_backoff > Duration::from_secs(60)
            || self.outage_budget > Duration::from_secs(120)
            || self.window > Duration::from_secs(3600)
            || (self.max_attempts > 0 && (self.max_recoveries == 0 || self.outage_budget.is_zero()))
        {
            return Err(Error::InvalidConfiguration);
        }
        Ok(())
    }
    fn backoff(self, completed_attempts: u32) -> Duration {
        let doublings = completed_attempts.saturating_sub(1).min(16);
        self.first_backoff
            .saturating_mul(1 << doublings)
            .min(self.max_backoff)
    }
}

/// How far an accepted request had progressed when its connection ended.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Stage {
    /// Input was still being sent.
    InputOpen,
    /// Input ended; no output had arrived.
    AwaitingResponse,
    /// Some output arrived; generation had not completed.
    Responding,
    /// Generation completed and every earlier event was delivered.
    Generated,
}

/// Conversation state of a newly ready connection.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Context {
    /// First connection of this supervisor.
    Initial,
    /// Resumed from a point the service advertised as current, with no known
    /// input accepted on a later connection. This is not an acknowledgement
    /// that every client message or the latest exchange is in model context.
    Resumed,
    /// The last point was invalidated, or accepted input on a later connection
    /// is known to postdate it. The exact missing context is not determined.
    ResumedBeforeLatest,
    /// Reconnected without a handle. Earlier conversation is not available.
    Fresh,
}

#[derive(Debug)]
pub enum Notice {
    /// Setup completed. `after` is the error that ended the previous
    /// connection; it is absent for the first one. `elapsed` runs from that
    /// loss (or from the first attempt) to completed setup.
    Ready {
        session: SessionId,
        context: Context,
        attempts: u32,
        elapsed: Duration,
        after: Option<Error>,
    },
    Event(Event),
    /// The connection failed. Input is refused until the next `Ready`. Output
    /// received before the failure may still follow as events.
    Unavailable {
        error: Error,
    },
    /// The failed connection's output is exhausted and this request's
    /// generation had completed: its whole answer was delivered. Only the idle
    /// barrier was outstanding, and a closed connection is that barrier.
    Settled {
        lineage: Lineage,
        error: Error,
    },
    /// The failed connection's output is exhausted and this accepted request
    /// was unfinished. Its input is not replayed and no answer is requested
    /// again.
    TurnLost {
        lineage: Lineage,
        stage: Stage,
        error: Error,
    },
}

/// A control-plane observation, independent of the ordered answer stream.
/// Revisions are strictly increasing within one supervisor; `session` identifies
/// the connection or attempt, never an answer owner. Times use the local Tokio
/// monotonic clock and retain their source observation time across slow polling.
/// Locally enforced budget exhaustion is stamped when actually observed, never
/// backdated to the mathematical deadline; that deadline still bounds recovery.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LinkUpdate {
    pub revision: u64,
    pub session: SessionId,
    pub observed_at: Instant,
    pub kind: LinkUpdateKind,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LinkUpdateKind {
    Ready {
        context: Context,
        attempts: u32,
        started_at: Instant,
        completed_at: Instant,
        outage_started_at: Option<Instant>,
        after: Option<Error>,
    },
    Unavailable {
        error: Error,
    },
    /// Close admission now. This does not discard the old answer: its events
    /// and turn outcome precede the terminal error from `next_output`.
    Terminal {
        failure: Failure,
    },
}

/// Ordered output only. No link observation consumes output capacity.
#[derive(Debug)]
pub enum OutputNotice {
    Event(Event),
    Settled {
        lineage: Lineage,
        error: Error,
    },
    TurnLost {
        lineage: Lineage,
        stage: Stage,
        error: Error,
    },
}
impl From<OutputNotice> for Notice {
    fn from(output: OutputNotice) -> Self {
        match output {
            OutputNotice::Event(event) => Self::Event(event),
            OutputNotice::Settled { lineage, error } => Self::Settled { lineage, error },
            OutputNotice::TurnLost {
                lineage,
                stage,
                error,
            } => Self::TurnLost {
                lineage,
                stage,
                error,
            },
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FailureReason {
    /// Local shutdown was requested.
    Shutdown,
    /// The first connection failed.
    InitialConnect,
    /// The error class does not allow an immediate reconnect.
    NotRecoverable,
    /// Policy requires conversation context and no handle is available.
    NoResumableContext,
    AttemptsExhausted,
    BudgetExhausted,
    /// Too many recoveries inside the policy window.
    Unstable,
    /// The connection task ended without reporting a terminal state.
    Lifecycle,
}

/// Terminal result. `error` is the fixed category that ended the last
/// connection or attempt; no raw server text is carried.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Failure {
    pub error: Error,
    pub reason: FailureReason,
    pub attempts: u32,
}
impl fmt::Display for Failure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} ({:?} after {} recovery attempt(s))",
            self.error, self.reason, self.attempts
        )
    }
}
impl std::error::Error for Failure {}

#[derive(Clone, Copy)]
struct Tracked {
    lineage: Lineage,
    stage: Stage,
}
#[derive(Clone, Copy)]
struct Outage {
    since: Instant,
    error: Error,
    /// Whether the handle offered for this outage was current when lost.
    context: Context,
}
struct Live {
    connection: Connection,
    input: InputSender,
    session: SessionId,
    state: watch::Receiver<State>,
}
/// A failed connection that still holds output received before it failed.
struct Drain {
    connection: Connection,
    error: Error,
    /// The request that was in flight on it.
    tracked: Cell<Option<Tracked>>,
}
/// A connection attempt running on the runtime. It must advance whether or
/// not the caller is asking for notices: a caller playing a long buffered
/// answer at speaking speed asks rarely.
struct AttemptResult {
    result: Result<Connection>,
    completed_at: Instant,
}
struct Dialing(JoinHandle<AttemptResult>);
impl Dialing {
    fn start(attempt: Attempt) -> Self {
        Self(tokio::spawn(async move {
            let result = attempt.await;
            AttemptResult {
                result,
                completed_at: Instant::now(),
            }
        }))
    }
}
impl Drop for Dialing {
    fn drop(&mut self) {
        self.0.abort();
    }
}
enum Link {
    Connecting {
        attempt: Dialing,
        session: SessionId,
        number: u32,
        started: Instant,
        outage: Option<Outage>,
    },
    Waiting {
        until: Instant,
        number: u32,
        outage: Outage,
    },
    Connected(Live),
    Failed(Failure),
}
enum Progress {
    Connected {
        connection: Connection,
        session: SessionId,
        number: u32,
        started: Instant,
        outage: Option<Outage>,
    },
    AttemptFailed {
        error: Error,
        observed_at: Instant,
        number: u32,
        outage: Option<Outage>,
    },
    Disconnected,
    Redial {
        number: u32,
        outage: Outage,
    },
    BudgetExhausted {
        number: u32,
        outage: Outage,
    },
}
/// Observe a pending attempt or backoff. Never resolves for a link that is
/// connected or has failed. Cancellation-safe: the attempt keeps running.
async fn progress(link: &mut Link, policy: &RecoveryPolicy) -> Progress {
    match link {
        Link::Connecting {
            attempt,
            session,
            number,
            started,
            outage,
        } => {
            let (session, number, started, outage) = (*session, *number, *started, *outage);
            let joined = match outage {
                // The first connection is bounded by its own timeouts.
                None => (&mut attempt.0).await,
                Some(outage) => {
                    let deadline = outage.since + policy.outage_budget;
                    // Harvest completed attempts before examining the current
                    // clock; the retained completion stamp decides the budget.
                    tokio::select! {
                        biased;
                        joined = &mut attempt.0 => joined,
                        _ = tokio::time::sleep_until(deadline) => {
                            return Progress::BudgetExhausted { number, outage };
                        }
                    }
                }
            };
            // A task fault has no successful source stamp. Its observation is
            // made here and can never be backdated into the outage budget.
            let AttemptResult {
                result,
                completed_at,
            } = joined.unwrap_or(AttemptResult {
                result: Err(Error::Transport),
                completed_at: Instant::now(),
            });
            let observed_at = result.as_ref().map_or(completed_at, Connection::ready_at);
            if let Some(outage) = outage
                && observed_at >= outage.since + policy.outage_budget
            {
                return Progress::BudgetExhausted { number, outage };
            }
            match result {
                Ok(connection) => Progress::Connected {
                    connection,
                    session,
                    number,
                    started,
                    outage,
                },
                Err(error) => Progress::AttemptFailed {
                    error,
                    observed_at,
                    number,
                    outage,
                },
            }
        }
        Link::Waiting {
            until,
            number,
            outage,
        } => {
            tokio::time::sleep_until(*until).await;
            if Instant::now() >= outage.since + policy.outage_budget {
                return Progress::BudgetExhausted {
                    number: *number,
                    outage: *outage,
                };
            }
            Progress::Redial {
                number: *number,
                outage: *outage,
            }
        }
        Link::Connected(live) => {
            let _ = live.state.wait_for(|state| *state != State::Ready).await;
            Progress::Disconnected
        }
        Link::Failed(_) => std::future::pending().await,
    }
}
fn observe(tracked: &Cell<Option<Tracked>>, event: &Event) {
    let Some(mut request) = tracked.get() else {
        return;
    };
    let stage = match event {
        Event::Audio { lineage, .. } | Event::ModelText { lineage, .. }
            if *lineage == request.lineage =>
        {
            Some(Stage::Responding)
        }
        Event::OutputTranscript { lineage, .. }
            if *lineage == request.lineage && request.stage == Stage::AwaitingResponse =>
        {
            Some(Stage::Responding)
        }
        Event::GenerationComplete { lineage } if *lineage == request.lineage => {
            Some(Stage::Generated)
        }
        Event::TurnComplete {
            lineage,
            idle: true,
        } if *lineage == request.lineage => {
            tracked.set(None);
            return;
        }
        _ => None,
    };
    if let Some(stage) = stage {
        request.stage = stage;
        tracked.set(Some(request));
    }
}

pub struct Supervisor<C> {
    connector: C,
    policy: RecoveryPolicy,
    sessions: u64,
    link: Link,
    draining: Option<Drain>,
    /// The request in flight on the live connection. Updated from the input
    /// methods (shared access) and from events.
    tracked: Cell<Option<Tracked>>,
    /// The last successfully submitted input, retained after local retirement
    /// so its End may still close that same connection's open activity. It also
    /// prevents an old request from being submitted to a replacement session.
    submitted: Cell<Option<Lineage>>,
    retired_through: Cell<u64>,
    resume: Option<ResumptionPoint>,
    recoveries: VecDeque<Instant>,
    /// Exactly one unconsumed control update and one ordered drain outcome.
    pending_link: Option<LinkUpdate>,
    pending_output: Option<OutputNotice>,
    revision: u64,
    failure_observed_at: Option<Instant>,
    terminal_announced: bool,
}

impl<C: Connect> Supervisor<C> {
    /// Starts the first connection attempt on the current runtime. Its result
    /// is reported by [`Self::next`].
    pub fn new(mut connector: C, policy: RecoveryPolicy) -> Result<Self> {
        policy.validate()?;
        let session = SessionId::new(1)?;
        let started = Instant::now();
        let attempt = Dialing::start(connector.connect(session, None));
        Ok(Self {
            connector,
            policy,
            sessions: 1,
            link: Link::Connecting {
                attempt,
                session,
                number: 1,
                started,
                outage: None,
            },
            draining: None,
            tracked: Cell::new(None),
            submitted: Cell::new(None),
            retired_through: Cell::new(0),
            resume: None,
            recoveries: VecDeque::new(),
            pending_link: None,
            pending_output: None,
            revision: 0,
            failure_observed_at: None,
            terminal_announced: false,
        })
    }

    /// A connection is ready and has not failed. Input is accepted.
    pub fn is_connected(&self) -> bool {
        self.input().is_ok()
    }

    fn input(&self) -> Result<(&InputSender, SessionId)> {
        match &self.link {
            // A failure this supervisor has not acted on yet still refuses.
            Link::Connected(live) if live.connection.state() == State::Ready => {
                Ok((&live.input, live.session))
            }
            _ => Err(Error::NotConnected),
        }
    }

    /// See [`InputSender::try_start`]. Fails with [`Error::NotConnected`]
    /// while no connection is ready; accepted input is never buffered here.
    pub fn try_start(&self, request: RequestId) -> Result<()> {
        let (input, session) = self.input()?;
        if request.get() <= self.retired_through.get()
            || self
                .submitted
                .get()
                .is_some_and(|previous| request <= previous.request)
        {
            return Err(Error::StaleRequest);
        }
        input.try_start(request)?;
        // InputSender retires output on its own connection. Do the same for
        // the old connection that may still be draining after a reconnect.
        // Only an accepted Start may supersede that earlier answer.
        if let Ok(previous) = RequestId::new(request.get() - 1) {
            self.retire(previous);
        }
        let lineage = Lineage { session, request };
        self.submitted.set(Some(lineage));
        self.tracked.set(Some(Tracked {
            lineage,
            stage: Stage::InputOpen,
        }));
        Ok(())
    }
    /// Narrow evidence for classifying a stale handoff, not an ownership grant.
    /// True only for the exact last successfully submitted request when a ready
    /// replacement connection now has a different session. In particular, it is
    /// false while reconnecting and for unrelated or never-submitted requests.
    pub fn input_belongs_to_replaced_connection(&self, request: RequestId) -> bool {
        self.submitted.get().is_some_and(|submitted| {
            submitted.request == request
                && self
                    .input()
                    .is_ok_and(|(_, session)| session != submitted.session)
        })
    }

    fn submitted_input(&self, request: RequestId) -> Result<&InputSender> {
        let (input, session) = self.input()?;
        if self.submitted.get() != Some(Lineage { session, request }) {
            return Err(Error::StaleRequest);
        }
        Ok(input)
    }
    pub fn try_audio(
        &self,
        request: RequestId,
        sequence: u64,
        captured_at: std::time::Instant,
        samples: &[i16],
    ) -> Result<()> {
        self.submitted_input(request)?
            .try_audio(request, sequence, captured_at, samples)
    }
    pub fn try_end(&self, request: RequestId) -> Result<()> {
        self.submitted_input(request)?.try_end(request)?;
        if let Some(mut tracked) = self.tracked.get()
            && tracked.lineage.request == request
            && tracked.stage == Stage::InputOpen
        {
            tracked.stage = Stage::AwaitingResponse;
            self.tracked.set(Some(tracked));
        }
        Ok(())
    }
    /// Synchronous local retirement. It is remembered across a reconnect,
    /// applies to output still held by a failed connection, and never waits
    /// for the network or for a connection to exist.
    pub fn retire(&self, request: RequestId) {
        self.retired_through
            .set(self.retired_through.get().max(request.get()));
        let cancelled = |tracked: &Tracked| tracked.lineage.request <= request;
        if self.tracked.get().as_ref().is_some_and(cancelled) {
            // Cancelled locally: nothing is owed to it any more.
            self.tracked.set(None);
        }
        if let Some(drain) = &self.draining {
            if drain.tracked.get().as_ref().is_some_and(cancelled) {
                drain.tracked.set(None);
            }
            drain.connection.input().retire(request);
        }
        if let Link::Connected(live) = &self.link {
            live.input.retire(request);
        }
    }
    pub fn shutdown(&mut self) {
        if let Link::Connected(live) = &self.link {
            live.input.shutdown();
        }
        if let Some(drain) = self.draining.take() {
            drain.connection.shutdown();
        }
        self.tracked.set(None);
        self.pending_link = None;
        self.pending_output = None;
        self.fail_at(Error::Closed, FailureReason::Shutdown, 0, Instant::now());
    }

    /// Nonblocking upkeep shared by legacy and split consumers. At most one
    /// link transition is applied. A single pending notification stops further
    /// application of link transitions, never the already spawned dial task.
    /// Consume it with `poll_link` or `next`; no PCM is read here.
    pub fn maintain(&mut self) {
        // A queued Ready can become false before its consumer polls. Never
        // reopen admission using it; retain the real failure observation instead.
        if self
            .pending_link
            .is_some_and(|update| matches!(update.kind, LinkUpdateKind::Ready { .. }))
            && matches!(&self.link, Link::Connected(live) if live.connection.state() != State::Ready)
        {
            self.pending_link = None;
        }
        if self.pending_link.is_some() {
            return;
        }
        match &self.link {
            Link::Connected(live) if live.connection.state() != State::Ready => self.disconnected(),
            Link::Failed(failure) if !self.terminal_announced => {
                let failure = *failure;
                self.terminal_announced = true;
                self.publish(
                    self.failure_observed_at.unwrap_or_else(Instant::now),
                    LinkUpdateKind::Terminal { failure },
                );
            }
            _ => {
                if let Some(step) = progress(&mut self.link, &self.policy).now_or_never() {
                    self.apply(step);
                }
            }
        }
    }

    /// Take one availability update without consuming answer data. Split
    /// consumers MUST call this regularly (for example on their bounded control
    /// service tick), even when no PCM credit is available. Never mix `next()`
    /// with the split `poll_link()` / `next_output()` API.
    ///
    /// One fixed notification slot bounds storage. While it is occupied no
    /// further transition is applied. A stale pending Ready is superseded by
    /// the observed failure, so revisions may skip but never regress. A remote
    /// Close hidden behind the session's deliberate WebSocket PCM read gate is
    /// not yet observed; this API neither bypasses that bound nor invents time.
    pub fn poll_link(&mut self) -> Option<LinkUpdate> {
        self.maintain();
        self.pending_link.take()
    }

    /// Cancellation-safe ordered data plane. `poll_link` must be serviced
    /// separately and regularly: when a control notification is pending this
    /// future waits without consuming data or spinning. Cancel it on the next
    /// control tick, poll the link, then ask again. A terminal link notification
    /// does not truncate the buffered answer or its final outcome.
    pub async fn next_output(&mut self) -> std::result::Result<OutputNotice, Failure> {
        match self.output_step().await? {
            Some(output) => Ok(output),
            None => std::future::pending().await,
        }
    }

    /// Legacy, cancellation-safe combined consumer. Preserves the existing
    /// notice API and ordered drain semantics. Do not mix with split consumers.
    pub async fn next(&mut self) -> std::result::Result<Notice, Failure> {
        loop {
            if let Some(update) = self.poll_link() {
                match update.kind {
                    LinkUpdateKind::Ready {
                        context,
                        attempts,
                        started_at,
                        completed_at,
                        outage_started_at,
                        after,
                    } => {
                        return Ok(Notice::Ready {
                            session: update.session,
                            context,
                            attempts,
                            elapsed: completed_at
                                .duration_since(outage_started_at.unwrap_or(started_at)),
                            after,
                        });
                    }
                    LinkUpdateKind::Unavailable { error } => {
                        return Ok(Notice::Unavailable { error });
                    }
                    LinkUpdateKind::Terminal { .. } => {}
                }
            }
            if let Some(output) = self.output_step().await? {
                return Ok(output.into());
            }
        }
    }

    async fn output_step(&mut self) -> std::result::Result<Option<OutputNotice>, Failure> {
        loop {
            self.maintain();
            // Privacy/local stop is never blocked by a notification consumer.
            if let Link::Failed(failure) = &self.link
                && failure.reason == FailureReason::Shutdown
            {
                return Err(*failure);
            }
            if self.pending_link.is_some() {
                return Ok(None);
            }
            if let Some(output) = self.pending_output.take() {
                return Ok(Some(output));
            }
            if let Some(drain) = self.draining.as_mut() {
                let step = tokio::select! {
                    biased;
                    progress = progress(&mut self.link, &self.policy) => Err(progress),
                    event = drain.connection.next_event() => Ok(event),
                };
                match step {
                    Ok(Some(event)) => {
                        observe(&drain.tracked, &event);
                        return Ok(Some(OutputNotice::Event(event)));
                    }
                    Ok(None) => self.settle(),
                    Err(progress) => self.apply(progress),
                }
                continue;
            }
            match &mut self.link {
                Link::Failed(failure) => return Err(*failure),
                Link::Connected(live) => {
                    if live.connection.state() == State::Ready {
                        tokio::select! {
                            biased;
                            event = live.connection.next_event() => {
                                if let Some(event) = event {
                                    observe(&self.tracked, &event);
                                    return Ok(Some(OutputNotice::Event(event)));
                                }
                            }
                            _ = live.state.wait_for(|state| *state != State::Ready) => {}
                        }
                    }
                    self.disconnected();
                }
                _ => {
                    let progress = progress(&mut self.link, &self.policy).await;
                    self.apply(progress);
                }
            }
        }
    }

    fn apply(&mut self, progress: Progress) {
        match progress {
            Progress::Connected {
                connection,
                session,
                number,
                started,
                outage,
            } => {
                self.connected(connection, session, number, started, outage);
            }
            Progress::AttemptFailed {
                error,
                observed_at,
                number,
                outage,
            } => {
                self.attempt_failed(error, observed_at, number, outage);
            }
            Progress::Disconnected => self.disconnected(),
            Progress::Redial { number, outage } => self.dial(number, Some(outage)),
            Progress::BudgetExhausted { number, outage } => {
                self.fail_at(
                    outage.error,
                    FailureReason::BudgetExhausted,
                    number,
                    Instant::now(),
                );
            }
        }
    }

    fn publish(&mut self, observed_at: Instant, kind: LinkUpdateKind) -> bool {
        debug_assert!(self.pending_link.is_none());
        let Some(revision) = self.revision.checked_add(1) else {
            self.fail_at(
                Error::InvalidConfiguration,
                FailureReason::Lifecycle,
                0,
                Instant::now(),
            );
            // No revision can represent another update. Refuse input and end
            // the ordered stream; never reuse a status identity.
            self.terminal_announced = true;
            return false;
        };
        self.revision = revision;
        self.pending_link = Some(LinkUpdate {
            revision,
            session: SessionId::new(self.sessions).expect("nonzero session counter"),
            observed_at,
            kind,
        });
        true
    }

    fn fail_at(
        &mut self,
        error: Error,
        reason: FailureReason,
        attempts: u32,
        observed_at: Instant,
    ) {
        self.link = Link::Failed(Failure {
            error,
            reason,
            attempts,
        });
        self.failure_observed_at = Some(observed_at);
        self.terminal_announced = false;
    }

    fn dial(&mut self, number: u32, outage: Option<Outage>) {
        let Some(next) = self.sessions.checked_add(1) else {
            return self.fail_at(
                Error::InvalidConfiguration,
                FailureReason::Lifecycle,
                number,
                Instant::now(),
            );
        };
        let Ok(session) = SessionId::new(next) else {
            return self.fail_at(
                Error::InvalidConfiguration,
                FailureReason::Lifecycle,
                number,
                Instant::now(),
            );
        };
        self.sessions = next;
        let handle = self.resume.as_ref().map(|point| point.handle().clone());
        let started = Instant::now();
        self.link = Link::Connecting {
            attempt: Dialing::start(self.connector.connect(session, handle)),
            session,
            number,
            started,
            outage,
        };
    }

    fn connected(
        &mut self,
        connection: Connection,
        session: SessionId,
        number: u32,
        started: Instant,
        outage: Option<Outage>,
    ) {
        let completed_at = connection.ready_at();
        let input = connection.input();
        if let Ok(retired) = RequestId::new(self.retired_through.get()) {
            input.retire(retired);
        }
        if outage.is_some() {
            self.recoveries.push_back(completed_at);
        }
        let state = connection.subscribe_state();
        self.link = Link::Connected(Live {
            connection,
            input,
            session,
            state,
        });
        if !self.is_connected() {
            self.disconnected();
            return;
        }
        self.publish(
            completed_at,
            LinkUpdateKind::Ready {
                context: outage.map_or(Context::Initial, |outage| outage.context),
                attempts: number,
                started_at: started,
                completed_at,
                outage_started_at: outage.map(|outage| outage.since),
                after: outage.map(|outage| outage.error),
            },
        );
    }

    fn attempt_failed(
        &mut self,
        error: Error,
        observed_at: Instant,
        number: u32,
        outage: Option<Outage>,
    ) {
        let Some(mut outage) = outage else {
            return self.fail_at(error, FailureReason::InitialConnect, 0, observed_at);
        };
        // A refusal while presenting a handle may be the handle being refused.
        // Only a policy that tolerates a session without context may drop it.
        let drop_handle = self.resume.is_some()
            && !self.policy.require_context
            && matches!(
                error,
                Error::ServerRejected | Error::UnexpectedResponse | Error::PeerClosed { .. }
            );
        match error.recovery() {
            Recovery::Reconnect => {}
            Recovery::Fatal if drop_handle => {}
            Recovery::Fatal | Recovery::Unavailable => {
                return self.fail_at(error, FailureReason::NotRecoverable, number, observed_at);
            }
        }
        if drop_handle {
            self.resume = None;
            outage.context = Context::Fresh;
        }
        if number >= self.policy.max_attempts {
            return self.fail_at(error, FailureReason::AttemptsExhausted, number, observed_at);
        }
        let until = observed_at + self.policy.backoff(number);
        if until >= outage.since + self.policy.outage_budget {
            return self.fail_at(error, FailureReason::BudgetExhausted, number, observed_at);
        }
        self.link = Link::Waiting {
            until,
            number: number + 1,
            outage,
        };
    }

    /// The live connection failed or its event stream ended. Report it now,
    /// keep its buffered output flowing, and start replacing it at once.
    fn disconnected(&mut self) {
        let placeholder = Link::Failed(Failure {
            error: Error::Closed,
            reason: FailureReason::Lifecycle,
            attempts: 0,
        });
        let Link::Connected(live) = std::mem::replace(&mut self.link, placeholder) else {
            return;
        };
        let (state, observed_at) = live.connection.observed_state();
        let error = match state {
            State::Disconnected(error) => error,
            State::Closed => {
                self.tracked.set(None);
                return self.fail_at(Error::Closed, FailureReason::Shutdown, 0, observed_at);
            }
            // The stream ended with no terminal state: already a lifecycle
            // fault through the placeholder above.
            State::Ready => {
                self.fail_at(
                    Error::Transport,
                    FailureReason::Lifecycle,
                    0,
                    Instant::now(),
                );
                return;
            }
        };
        if let Some(point) = live.connection.resumption() {
            self.resume = Some(point);
        } else if live.connection.resumption_invalidated()
            && let Some(point) = self.resume.as_mut()
        {
            // A resumed connection need not issue its own handle before
            // announcing that the current state cannot be resumed.
            point.invalidate();
        }
        let announced = self.publish(observed_at, LinkUpdateKind::Unavailable { error });
        let tracked = self.tracked.take();
        // A replacement can fail before accepting input, while the original
        // answer is still being consumed. Keep that answer and its terminal
        // outcome instead of replacing the one bounded drain slot. If this
        // connection accepted newer input, try_start already retired the old
        // drain, so replacing it cannot lose still-owned output.
        if tracked.is_some() || self.draining.is_none() {
            debug_assert!(
                self.draining
                    .as_ref()
                    .is_none_or(|drain| drain.tracked.get().is_none())
            );
            self.draining = Some(Drain {
                connection: live.connection,
                error,
                tracked: Cell::new(tracked),
            });
        }
        if announced {
            self.recover(error, observed_at);
        }
    }

    /// Every event the failed connection produced has been returned. Say what
    /// became of the request that was in flight on it.
    fn settle(&mut self) {
        let Some(drain) = self.draining.take() else {
            return;
        };
        let Some(tracked) = drain.tracked.get() else {
            return;
        };
        // Its identifier ends with that connection: a later session must not
        // accept the same request again as if nothing had happened.
        self.retired_through.set(
            self.retired_through
                .get()
                .max(tracked.lineage.request.get()),
        );
        if let Link::Connected(live) = &self.link {
            live.input.retire(tracked.lineage.request);
        }
        debug_assert!(self.pending_output.is_none());
        self.pending_output = Some(match tracked.stage {
            Stage::Generated => OutputNotice::Settled {
                lineage: tracked.lineage,
                error: drain.error,
            },
            stage => OutputNotice::TurnLost {
                lineage: tracked.lineage,
                stage,
                error: drain.error,
            },
        });
    }

    fn recover(&mut self, error: Error, observed_at: Instant) {
        if self.policy.max_attempts == 0 || error.recovery() != Recovery::Reconnect {
            return self.fail_at(error, FailureReason::NotRecoverable, 0, observed_at);
        }
        let context = match &self.resume {
            Some(point)
                if point.is_current()
                    && self
                        .submitted
                        .get()
                        .is_none_or(|input| input.session <= point.advertised_session()) =>
            {
                Context::Resumed
            }
            Some(_) => Context::ResumedBeforeLatest,
            None if self.policy.require_context => {
                return self.fail_at(error, FailureReason::NoResumableContext, 0, observed_at);
            }
            None => Context::Fresh,
        };
        let now = Instant::now();
        while self
            .recoveries
            .front()
            .is_some_and(|at| now.duration_since(*at) >= self.policy.window)
        {
            self.recoveries.pop_front();
        }
        if self.recoveries.len() >= self.policy.max_recoveries as usize {
            return self.fail_at(error, FailureReason::Unstable, 0, observed_at);
        }
        if now >= observed_at + self.policy.outage_budget {
            return self.fail_at(error, FailureReason::BudgetExhausted, 0, now);
        }
        self.dial(
            1,
            Some(Outage {
                since: observed_at,
                error,
                context,
            }),
        );
    }
}

#[cfg(test)]
mod tests;
