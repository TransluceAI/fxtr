// The longest reply of a conversation (`LongestReply` in `scripted_rollout.py`): its text, and a
// link to the conversation with a selector for the reply, so the conversation view opens marking
// it. A link with params opens the conversation alone, fixed.

import styles from "./rollouts.module.css";
import { EntityLink, unwrap, useEntity, type EntityData, type EntityID, type EntityPanelProps } from "fxtr-view";
import type { MessageSelector } from "fxtr-view-behaviors";

export const LONGEST_REPLY = "org.transluce.examples.behavior_rollouts.LongestReply.v1";

/** `LongestReply`'s stored shape. */
export interface LongestReplyData extends EntityData {
  readonly rollout: EntityID;
  readonly selector: MessageSelector;
  readonly text: string;
}

export function LongestReplyPanel({ id }: EntityPanelProps) {
  const data = unwrap(useEntity(id as EntityID<LongestReplyData>));
  return (
    <section className={styles["longest"]}>
      <header className={styles["muted"]}>Longest reply · message {data.selector.message + 1} of the conversation</header>
      <p>{data.text}</p>
      <div className={styles["reference"]}>
        In the conversation: <EntityLink id={data.rollout} params={{ selector: data.selector }} />
      </div>
    </section>
  );
}
