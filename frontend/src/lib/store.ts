import { useCallback, useLayoutEffect, useRef, useSyncExternalStore } from 'react'

/**
 * A value held outside React state, for state that changes often but that only a few components
 * read: the writer does not re-render, and each reader re-renders only when the part of the
 * value it selects changes (`useStoreSelector`). Setting a value `equals` the current one does
 * nothing.
 */
export class Store<T> {
  private current: T
  private readonly listeners = new Set<() => void>()

  constructor(initial: T, private readonly equals: (a: T, b: T) => boolean = Object.is) {
    this.current = initial
  }

  get = (): T => this.current

  set(next: T) {
    if (this.equals(this.current, next)) return
    this.current = next
    for (const listener of this.listeners) listener()
  }

  update(change: (current: T) => T) {
    this.set(change(this.current))
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
}

/** The part of the store's value `select` picks, re-rendering only when that changes (by
 *  `Object.is`, so `select` returns a primitive or a part of the value, not a new object). */
export function useStoreSelector<T, S>(store: Store<T>, select: (value: T) => S): S {
  return useSyncExternalStore(store.subscribe, () => select(store.get()))
}

/** A function of fixed identity that calls the latest `fn`, for a callback prop of a memoized
 *  component. Not for calling during render. */
export function useStableCallback<A extends unknown[], R>(fn: (...args: A) => R): (...args: A) => R {
  const latest = useRef(fn)
  useLayoutEffect(() => {
    latest.current = fn
  })
  return useCallback((...args: A) => latest.current(...args), [])
}
