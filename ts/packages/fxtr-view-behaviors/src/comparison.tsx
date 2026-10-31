import styles from "./behaviors.module.css";
import { EntityDataError, EntityLink, unwrap, useEntity, type EntityID } from "fxtr-view";
import { Initialization, statusLabel } from "./entities";
import { Messages, MetadataList, usageOf } from "./messages";
import { CHAT_ROLLOUT, type ChatRolloutData, type TurnData } from "./types";

function useRollout(id: EntityID): ChatRolloutData {
  const data = unwrap(useEntity(id));
  if (data.$type !== CHAT_ROLLOUT) throw new EntityDataError("Expected a ChatRollout for comparison");
  return data as ChatRolloutData;
}

function Answer({ turn }: { turn: TurnData | undefined }) {
  if (!turn) return <p className={styles["muted"]}>No response recorded.</p>;
  return <>
    <Messages messages={turn.messages} />
    <MetadataList entries={turn.metadata} label="Response details" />
  </>;
}

/** Each pair of answers shares a grid row, so different response lengths cannot shift later turns. */
export function ComparedRollouts({ left, right }: {
  left: { model: string; id: EntityID };
  right: { model: string; id: EntityID };
}) {
  const leftData = useRollout(left.id);
  const rightData = useRollout(right.id);
  const columns = [{ ...left, data: leftData }, { ...right, data: rightData }];
  const turnCount = Math.max(...columns.flatMap(({ data }) => [data.context_turns.length, data.model_turns.length]));
  return (
    <div className={styles["comparison"]} aria-label="Turn-by-turn comparison">
      <div className={`${styles["compare-columns"]} ${styles["compare-head"]}`}>
        {columns.map(({ model, data }, index) => (
          <div key={index}>
            <h3>{model}</h3>
            <div className={data.status.kind === "rollout_failed" ? styles["error"] : styles["muted"]}>
              {statusLabel(data.status)} · {data.model_turns.length} responses
            </div>
          </div>
        ))}
      </div>
      <div className={styles["compare-columns"]}>
        {columns.map(({ data }, index) => <Initialization key={index} data={data.initialization} />)}
      </div>
      {columns.some(({ data }) => data.status.kind === "rollout_failed") && (
        <div className={styles["compare-columns"]}>
          {columns.map(({ data }, index) => <div key={index}>
            {data.status.kind === "rollout_failed" && <p className={styles["error"]}>{data.status.failure.description}</p>}
          </div>)}
        </div>
      )}
      {turnCount === 0 && <p className={styles["muted"]}>No turns recorded.</p>}
      {Array.from({ length: turnCount }, (_, index) => {
        const leftContext = leftData.context_turns[index];
        const rightContext = rightData.context_turns[index];
        const shared = leftContext && rightContext &&
          JSON.stringify(leftContext.messages) === JSON.stringify(rightContext.messages);
        return (
          <section className={styles["compare-turn"]} aria-label={`Comparison turn ${index + 1}`} key={index}>
            <h3 className={styles["turn-number"]}>Turn {index + 1}</h3>
            {shared ? <div className={styles["shared-context"]}><Messages messages={leftContext.messages} /></div>
              : <div className={styles["compare-columns"]}>
                  {[leftContext, rightContext].map((context, side) => <div key={side}>
                    {context ? <Messages messages={context.messages} /> : <p className={styles["muted"]}>No context recorded.</p>}
                  </div>)}
                </div>}
            <div className={styles["compare-columns"]}>
              {columns.map(({ model, data }, side) => (
                <section className={styles["compare-answer"]} aria-label={`${model} · Turn ${index + 1}`} key={side}>
                  <Answer turn={data.model_turns[index]} />
                </section>
              ))}
            </div>
          </section>
        );
      })}
      <div className={`${styles["compare-columns"]} ${styles["reference"]}`}>
        {columns.map(({ data, id }, index) => <div key={index}>
          <p className={styles["muted"]}>{usageOf(data.model_turns)}</p>
          <EntityLink id={id} />
        </div>)}
      </div>
    </div>
  );
}
