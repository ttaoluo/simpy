"""
This module contains the implementation of SimPy's core classes. The
most important ones are directly importable via :mod:`simpy`.

"""
import types
from heapq import heappush, heappop
from inspect import isgenerator
from itertools import count

from simpy._compat import PY2

if PY2:
    import sys


Infinity = float('inf')  # Convenience alias for infinity

PENDING = object()       # Unique object to identify pending values of events

HIGH_PRIORITY = 0        # Priority of interrupts and Initialize events
DEFAULT_PRIORITY = 1     # Default priority used by events
LOW_PRIORITY = 2         # Priority of timeouts


class BoundClass(object):
    """Allows classes to behave like methods. The ``__get__()`` descriptor is
    basically identical to ``function.__get__()`` and binds the first argument
    of the ``cls`` to the descriptor instance."""

    def __init__(self, cls):
        self.cls = cls

    def __get__(self, obj, type=None):
        if obj is None:
            return self.cls
        return types.MethodType(self.cls, obj)

    @staticmethod
    def bind_early(instance):
        cls = type(instance)
        for name, obj in cls.__dict__.items():
            if type(obj) is BoundClass:
                bound_class = getattr(instance, name)
                setattr(instance, name, bound_class)


class Interrupt(Exception):
    """This exceptions is sent into a process if it was interrupted by
    another process (see :func:`Process.interrupt()`).

    ``cause`` may be none of no cause was explicitly passed to
    :func:`Process.interrupt()`.

    An interrupt has a higher priority as a normal event. Thus, if
    a process has a normal event and an interrupt scheduled at the same
    time, the interrupt will always be thrown into the PEM first.

    If a process is interrupted multiple times at the same time, all
    interrupts will be thrown into the PEM in the same order as they
    occurred.

    """
    def __init__(self, cause):
        super(Interrupt, self).__init__(cause)

    def __str__(self):
        return '%s(%r)' % (self.__class__.__name__, self.cause)

    @property
    def cause(self):
        """Property that returns the cause of an interrupt or ``None``
        if no cause was passed."""
        return self.args[0]


class Event(object):
    """Base class for all events.

    Every event is bound to an :class:`Environment` ``env`` and has a
    list of ``callbacks`` that are called when the event is processed.

    A callback can be any callable that accepts the following arguments:

    - *event:* The :class:`Event` instance the callback was registered
      at.
    - *success:* Boolean that indicates if the event was successful.
      A process that raised an uncaught exception might for example
      cause an unsuccessful (failed) event.
    - *value:* An event can optionally send an arbitrary value. It
      defaults to ``None``.

    You can add callbacks by appending them to the ``callbacks``
    attribute of an event.

    This class also implements ``__and__()`` (``&``) and ``__or__()``
    (``|``). If you concatenate two events using one of these operators,
    a :class:`Condition` event is generated that lets you wait for both
    or one of them.

    """
    def __init__(self, env, value=PENDING):
        self.env = env
        """The :class:`Environment` the event lives in."""
        self.callbacks = []
        """List of functions that are called when the event is
        processed."""
        self._value = value

    def __repr__(self):
        """Return the description of the event (see :meth:`_desc`) with the id
        of the event."""
        return '<%s object at 0x%x>' % (self._desc(), id(self))

    def _desc(self):
        """Return a string *Event()*."""
        return '%s()' % self.__class__.__name__

    @property
    def triggered(self):
        """Becomes ``True`` if the event has been triggered and its callbacks
        are about to be invoked."""
        return self._value is not PENDING

    @property
    def processed(self):
        """Becomes ``True`` if the event has been processed (e.g., its
        callbacks have been invoked)."""
        return self.callbacks is None

    @property
    def value(self):
        """Return the value of the event if it is available.

        The value is available when the event has been triggered.

        Raise a :exc:`RuntimeError` if the value is not yet available.

        """
        if self._value is PENDING:
            raise RuntimeError('Value of %s is not yet available' % self)
        return self._value

    def trigger(self, event):
        """Triggers the event with value of the provided ``event``.
        
        This method can be used directly as a callback function.
        
        """
        self.ok = event.ok
        self._value = event._value
        self.env.schedule(self, DEFAULT_PRIORITY)

    def succeed(self, value=None):
        """Schedule the event and mark it as successful.

        You can optionally pass an arbitrary ``value`` that will be sent
        into processes waiting for that event.

        Raise a :exc:`RuntimeError` if this event has already been
        scheduled.

        """
        if self._value is not PENDING:
            raise RuntimeError('%s has already been triggered' % self)

        self.ok = True
        self._value = value
        self.env.schedule(self, DEFAULT_PRIORITY)
        return self

    def fail(self, exception):
        """Schedule the event and mark it as failed.

        The ``exception`` will be thrown into processes waiting for that
        event.

        Raise a :exc:`ValueError` if ``exception`` is not an
        :exc:`Exception`.

        Raise a :exc:`RuntimeError` if this event has already been
        scheduled.

        """
        if not isinstance(exception, Exception):
            raise ValueError('%s is not an exception.' % exception)
        if self._value is not PENDING:
            raise RuntimeError('%s has already been triggered' % self)
        self.ok = False
        self._value = exception
        self.env.schedule(self, DEFAULT_PRIORITY)
        return self

    def __and__(self, other):
        return Condition(self.env, Condition.all_events, [self, other])

    def __or__(self, other):
        return Condition(self.env, Condition.any_events, [self, other])


class Condition(Event):
    """A *Condition* event groups several ``events`` and is triggered if
    a given condition (implemented by the ``evaluate`` function) becomes
    true.

    The value of the condition is a dictionary that maps the input
    events to their respective values. It only contains entries for
    those events that occurred until the condition was met.

    If one of the ``events`` fails, the condition also fails and
    forwards the exception of the failing event.

    The ``evaluate`` function receives the list of target events and the
    dictionary with all values currently available. If it returns
    ``True``, the condition is scheduled. The :func:`Condition.all_events()`
    and :func:`Condition.any_events()` functions are used to implement *and*
    (``&``) and *or* (``|``) for events.

    Since condition are normal events, too, they can also be used as
    sub- or nested conditions.

    """
    def __init__(self, env, evaluate, events):
        Event.__init__(self, env)
        self._evaluate = evaluate
        self._interim_values = {}
        self._events = []
        self._sub_conditions = []

        for event in events:
            self._add_event(event)

        # Register a callback which will update the value of this
        # condition once it is being processed.
        self.callbacks.append(self._collect_values)

        # Immediately trigger the condition if it is already met.
        if self._evaluate(self._events, self._interim_values):
            self.succeed({})

    def _desc(self):
        """Return a string *Condition(and_or_or, [events])*."""
        return '%s(%s, %s)' % (self.__class__.__name__,
                               self._evaluate.__name__, self._events)

    def _get_values(self):
        """Recursively collects the current values of all nested
        conditions into a flat dictionary."""
        values = dict(self._interim_values)

        for condition in self._sub_conditions:
            if condition in values:
                del values[condition]
            values.update(condition._get_values())

        return values

    def _collect_values(self, event):
        """Populates the final value of this condition."""
        if event.ok:
            self._value.update(self._get_values())

    def _add_event(self, event):
        """Add another event to the condition."""
        if self.env != event.env:
            raise RuntimeError('It is not allowed to mix events from '
                               'different environments')
        if self.callbacks is None:
            raise RuntimeError('Event %s has already been triggered' % self)
        if event.callbacks is None:
            raise RuntimeError('Event %s has already been triggered' % event)

        if isinstance(event, Condition):
            self._sub_conditions.append(event)

        self._events.append(event)
        event.callbacks.append(self._check)

        return self

    def _check(self, event):
        """Check if the condition was already met and schedule the event
        if so."""
        self._interim_values[event] = event._value

        if self._value is PENDING:
            if not event.ok:
                # Abort if the event has failed.
                event.defused = True
                self.fail(event._value)
            elif self._evaluate(self._events, self._interim_values):
                # The condition has been met. Schedule the event with an empty
                # dictionary as value. The _collect_values callback will
                # populate this dictionary once this condition gets processed.
                self.succeed({})

    def __iand__(self, other):
        if self._evaluate is not Condition.all_events:
            # Use self.__and__
            return NotImplemented

        return self._add_event(other)

    def __ior__(self, other):
        if self._evaluate is not Condition.any_events:
            # Use self.__or__
            return NotImplemented

        return self._add_event(other)

    @staticmethod
    def all_events(events, values):
        """A condition function that returns ``True`` if all ``events`` have
        been triggered."""
        return len(events) == len(values)

    @staticmethod
    def any_events(events, values):
        """A condition function that returns ``True`` if there is at least one
        of ``events`` has been triggered."""
        return len(values) > 0 or len(events) == 0


class AllOf(Condition):
    """A condition event that waits for all ``events``."""
    def __init__(self, env, events):
        Condition.__init__(self, env, Condition.all_events, events)


class AnyOf(Condition):
    def __init__(self, env, events):
        """A condition event that waits until the first of ``events`` is
        triggered."""
        Condition.__init__(self, env, Condition.any_events, events)


class Timeout(Event):
    """An event that is scheduled with a certain ``delay`` after its
    creation.

    This event can be used by processes to wait (or hold their state)
    for ``delay`` time steps. It is immediately scheduled at ``env.now
    + delay`` and has thus (in contrast to :class:`Event`) no
    *success()* or *fail()* method.

    """
    def __init__(self, env, delay, value=None):
        if delay < 0:
            raise ValueError('Negative delay %s' % delay)
        # NOTE: The following initialization code is inlined from
        # Event.__init__() for performance reasons.
        self.callbacks = []
        self.env = env
        self._delay = delay
        self.ok = True
        self._value = value
        env.schedule(self, LOW_PRIORITY, delay)

    def _desc(self):
        """Return a string *Timeout(delay[, value=value])*."""
        return '%s(%s%s)' % (self.__class__.__name__, self._delay,
                             '' if self._value is None else
                             (', value=%s' % self._value))


class Initialize(Event):
    """Initializes a process. Only used internally by :class:`Process`."""
    def __init__(self, env, process):
        self.env = env
        self.name = None
        self.ok = True
        self._value = None
        self.callbacks = [process._resume]
        env.schedule(self, HIGH_PRIORITY)


class Process(Event):
    """A *Process* is a wrapper for instantiated PEMs during their
    execution.

    A Processes has a generator (the generator that the PEM returns) and
    a reference to its :class:`Environment` ``env``. It also contains
    internal and external status information.  It is also used for
    process interaction, e.g., for interruptions.

    ``Process`` inherits :class:`Event`. You can thus wait for the
    termination of a process by simply yielding it from your PEM.

    An instance of this class is returned by
    :meth:`Environment.start()`.

    """
    def __init__(self, env, generator):
        if not isgenerator(generator):
            raise ValueError('%s is not a generator.' % generator)

        # NOTE: The following initialization code is inlined from
        # Event.__init__() for performance reasons.
        self.callbacks = []
        self.env = env
        self._generator = generator
        self._value = PENDING

        # Schedule the start of the execution of the process.
        self._target = Initialize(env, self)

    def _desc(self):
        """Return a string *Process(pem_name)*."""
        return '%s(%s)' % (self.__class__.__name__, self._generator.__name__)

    @property
    def target(self):
        """The event that the process is currently waiting for.

        May be ``None`` if the process was just started or interrupted
        and did not yet yield a new event.

        """
        return self._target

    @property
    def is_alive(self):
        """``True`` until the event has been processed."""
        return self._value is PENDING

    def interrupt(self, cause=None):
        """Interupt this process optionally providing a ``cause``.

        A process cannot be interrupted if it already terminated.
        A process can also not interrupt itself. Raise
        a :exc:`RuntimeError` in these cases.

        """
        if self._value is not PENDING:
            raise RuntimeError('%s has terminated and cannot be interrupted.' %
                               self)

        if self is self.env.active_process:
            raise RuntimeError('A process is not allowed to interrupt itself.')

        # Schedule interrupt event
        event = self.env.event(Interrupt(cause))
        event.ok = False
        # Interrupts do not cause the simulation to crash.
        event.defused = True
        event.callbacks.append(self._resume)
        self.env.schedule(event, HIGH_PRIORITY)

    def _resume(self, event):
        """Get the next event from this process and register as a callback.

        If the PEM generator exits or raises an exception, terminate
        this process. Also schedule this process to notify all
        registered callbacks, that the process terminated.

        """
        # Ignore dead processes. Multiple concurrently scheduled
        # interrupts cause this situation. If the process dies while
        # handling the first one, the remaining interrupts must be
        # discarded.
        if self._value is not PENDING:
            return

        # If the current target (e.g. an interrupt) isn't the one the process
        # expects, remove it from the original events joiners list.
        if self._target is not event:
            self._target.callbacks.remove(self._resume)

        # Mark the current process as active.
        self.env._active_proc = self

        while True:
            # Get next event from process
            try:
                if event.ok:
                    event = self._generator.send(event._value)
                else:
                    # The process has no choice but to handle the failed event
                    # (or fail itself).
                    event.defused = True
                    event = self._generator.throw(event._value)
            except StopIteration as e:
                # Process has terminated.
                event = None
                self.ok = True
                self._value = e.args[0] if len(e.args) else None
                self.env.schedule(self, DEFAULT_PRIORITY)
                break
            except BaseException as e:
                # Process has failed.
                event = None
                self.ok = False
                self._value = type(e)(*e.args)
                self._value.__cause__ = e
                if PY2:
                    self._value.__traceback__ = sys.exc_info()[2]
                self.env.schedule(self, DEFAULT_PRIORITY)
                break

            # Process returned another event to wait upon.
            try:
                # Be optimistic and blindly access the callbacks attribute.
                if event.callbacks is not None:
                    # The event has not yet been triggered. Register callback
                    # to resume the process if that happens.
                    event.callbacks.append(self._resume)
                    break
            except AttributeError:
                # Our optimism didn't work out, figure out what went wrong and
                # inform the user.
                if not hasattr(event, 'callbacks'):
                    msg = 'Invalid yield value "%s"' % event

                descr = _describe_frame(self._generator.gi_frame)
                error = RuntimeError('\n%s%s' % (descr, msg))
                # Drop the AttributeError as the cause for this exception.
                error.__cause__ = None
                raise error

        self._target = event
        self.env._active_proc = None


class EmptySchedule(Exception):
    """Thrown by the :class:`Environment` if there are no further events to be
    processed."""
    pass


class BaseEnvironment(object):
    """The abstract definition of an environment. An implementation must at
    least provide the means to access the current time in the environment (see
    :attr:`now`), to schedule (see :meth:`schedule()`) and execute (see
    :meth:`step()` and :meth:`run()`) events.

    The class is meant to be subclassed for different execution environments.
    :class:`Environment` is for example a simulation environment with a virtual
    time concept, whereas the :class:`~simpy.rt.RealtimeEnvironment` is
    schedules and executes events in real (e.g. wallclock) time."""

    @property
    def now(self):
        """Property that returns the current time in the environment."""
        raise NotImplementedError(self)

    def schedule(self, event, priority=DEFAULT_PRIORITY, delay=0):
        """Schedule an *event* with a given *priority* and a *delay*."""
        raise NotImplementedError(self)

    def step(self):
        """Process the next event."""
        raise NotImplementedError(self)

    def run(self, until=None):
        """Executes :meth:`step()` until the given criterion *until* is met.

        - If it is ``None`` (which is the default) this method will return if
          there are no further events to be processed.

        - If it is an :class:`Event` the method will continue stepping until
          this event has been triggered and returns its value.

        - If it can be converted to a number the method will continue stepping
          until the time in the environment reaches *until*.

        """
        if until is None:
            until = Event(self)
        elif not isinstance(until, Event):
            at = float(until)

            if at <= self.now:
                raise ValueError('until(=%s) should be > the current '
                        'simulation time.' % at)

            # Schedule the event with before all regular timeouts.
            until = Event(self)
            until._value = None
            self.schedule(until, HIGH_PRIORITY, at - self.now)

        until.callbacks.append(_stop_simulate)

        try:
            while True:
                self.step()
        except EmptySchedule:
            pass

        return until.value if until.triggered else None


class Environment(BaseEnvironment):
    """The simulation *environment* which simulates the passing of time by
    stepping from event to event.

    This class also provides aliases for common event types, for example:
    :attr:`process`, :attr:`timeout` and :attr:`event`."""

    def __init__(self, initial_time=0):
        self._now = initial_time
        self._queue = []
        """A list with all currently scheduled events."""
        self._eid = count()
        self._active_proc = None

        BoundClass.bind_early(self)

    @property
    def now(self):
        """Property that returns the current simulation time."""
        return self._now

    @property
    def active_process(self):
        """Property that returns the currently active process."""
        return self._active_proc

    process = BoundClass(Process)
    timeout = BoundClass(Timeout)
    event = BoundClass(Event)
    all_of = BoundClass(AllOf)
    any_of = BoundClass(AnyOf)
    suspend = event
    start = process

    def exit(self, value=None):
        """Convenience function provided for Python versions prior to 3.3. Stop
        the current process, optionally providing a ``value``.

        .. note::

            From Python 3.3, you can use ``return value`` instead."""
        raise StopIteration(value)

    def schedule(self, event, priority=DEFAULT_PRIORITY, delay=0):
        """Schedule an *event* with a given *priority* and a *delay*."""
        heappush(self._queue, (self._now + delay, priority, next(self._eid),
                              event))

    def peek(self):
        """Get the time of the next scheduled event. Return ``Infinity`` if
        there is no further event."""
        try:
            return self._queue[0][0]
        except IndexError:
            return Infinity

    def step(self):
        """Process the next event. If there are no further events an
        :exc:`EmptySchedule` will be risen."""
        try:
            self._now, _, _, event = heappop(self._queue)
        except IndexError:
            raise EmptySchedule()

        # Process callbacks of the event.
        for callback in event.callbacks:
            callback(event)
        event.callbacks = None

        if not event.ok:
            # The event has failed, check if it is defused. Raise the value if not.
            if not hasattr(event, 'defused'):
                raise event._value


def _describe_frame(frame):
    """Prints filename, linenumber and function name of a stackframe."""
    filename, name = frame.f_code.co_filename, frame.f_code.co_name
    lineno = frame.f_lineno

    with open(filename) as f:
        for no, line in enumerate(f):
            if no + 1 == lineno:
                break

    return '  File "%s", line %d, in %s\n    %s\n' % (filename, lineno, name,
                                                      line.strip())


def _stop_simulate(event):
    """Used as callback in :func:`simulate()` to stop the simulation when the
    *until* event occured."""
    raise EmptySchedule()
