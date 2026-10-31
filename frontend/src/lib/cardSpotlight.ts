import { createContext, useContext } from 'react'
import { Store, useStoreSelector } from './store'

/** The step a hovered chip has lit up, and whether its card is lit too (it was fully in view). */
export interface Spotlight {
  id: string
  card: boolean
}

export type SpotlightStore = Store<Spotlight | null>

export function createSpotlightStore(): SpotlightStore {
  return new Store<Spotlight | null>(null, (a, b) => a?.id === b?.id && a?.card === b?.card)
}

/**
 * Lighting up the step a hovered chip names. Its minimap row always lights; when its card is fully
 * on screen the card glows too, in place of the chip's hover preview, since the card itself says
 * more. Provided around the trace; where there is no card column to light (the flowchart's card),
 * chips always preview.
 */
export interface CardSpotlight {
  /** Light the step's minimap row, and its card if that is fully in view; say whether the card
   *  was lit. */
  light: (id: string) => boolean
  /** Put out the step's row and card, if it is the one lit. */
  clear: (id: string) => void
  /** What is lit, which the minimap's rows and the cards read. */
  spotlight: SpotlightStore
}

export const CardSpotlightContext = createContext<CardSpotlight | null>(null)

export function useCardSpotlight(): CardSpotlight | null {
  return useContext(CardSpotlightContext)
}

/** A spotlight on nothing, for a reader with none to subscribe to. */
export const NOTHING_LIT: SpotlightStore = createSpotlightStore()

/** Whether the step's card is lit; false outside the trace. */
export function useCardSpotlit(stepId: string): boolean {
  const store = useCardSpotlight()?.spotlight ?? NOTHING_LIT
  return useStoreSelector(store, (current) => current?.card === true && current.id === stepId)
}
