import { Suspense } from 'react'
import { stepStatus } from '../api/graphql'
import { useOverviewSnapshot, useStepPin } from '../api/live'
import type { InvocationStatus, StepInvocation } from '../types'
import { ErrorBoundary } from './ErrorBoundary'
import styles from './InvocationFailure.module.css'

/**
 * A failed invocation's error on one line, in its row of a card's table: `TypeName: message`, the
 * whole of it on hover (the row's modal shows it in full, `PendingInvocation`). The error is read
 * from the step's pin, or the child workflow's overview.
 */
export function FailureLine({ jobId, invocation }: { jobId: string; invocation: StepInvocation }) {
  return (
    <ErrorBoundary fallback={(error) => <Line text={`failed (its error did not load: ${error.message})`} />}>
      <Suspense fallback={<Line text="failed" />}>
        {invocation.stepCacheKey !== null ? (
          <StepFailure jobId={jobId} cacheKey={invocation.stepCacheKey} />
        ) : invocation.childPathname !== null ? (
          <WorkflowFailure jobId={jobId} pathname={invocation.childPathname} />
        ) : (
          <Line text="failed" />
        )}
      </Suspense>
    </ErrorBoundary>
  )
}

function StepFailure({ jobId, cacheKey }: { jobId: string; cacheKey: string }) {
  return <Line text={errorText(stepStatus(useStepPin(jobId, cacheKey)))} />
}

function WorkflowFailure({ jobId, pathname }: { jobId: string; pathname: string }) {
  return <Line text={errorText(useOverviewSnapshot(jobId, pathname).invocation)} />
}

const errorText = ({ error }: InvocationStatus) => (error ? `${error.typeName}: ${error.message}` : 'failed')

function Line({ text }: { text: string }) {
  return <span className={styles.line} title={text}>{text}</span>
}
