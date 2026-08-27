# Windows Controls

These wrappers were just a quick way to keep me from having to copy/paste commands over and over into powershell.

## START.bat

Starts the production research worker using the repository's `.venv`.

The worker remains attached to the terminal. Press `Ctrl+C` for a graceful
shutdown.

## STATUS.bat

Displays current SQLite job counts.

## RETRY_UNRESOLVED.bat

Requeues only these statuses:

- `CONFLICT`
- `UNFOUND`
- `TIMED_OUT`
- `ERROR`

Successful and likely classifications are not modified.

After requeueing, run `START.bat` to process the retry queue.

## EXPORT.bat

Exports the current SQLite research state to the configured Excel output.

## Design

The batch files intentionally contain very little application logic.
Queue management, validation, retry behavior, and persistence live in Python
so they can be tested independently of the Windows wrappers.
