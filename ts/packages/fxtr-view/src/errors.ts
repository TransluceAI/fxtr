// The library's error types. Every fault fxtr-view or a host package over it (fxtr-view-shell-util)
// raises itself is a `FxtrViewError`, so a shell or a test can tell the library's own faults from a
// renderer's.

/** Base class of every error fxtr-view and its host packages raise. */
export class FxtrViewError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

/** A value that should have been entity data (or an entity id) was not shaped like one. */
export class EntityDataError extends FxtrViewError {}

/** The view shell could not serve a request: no `ViewShellProvider` above, or a member the shell
 *  leaves out. */
export class ShellFault extends FxtrViewError {}
