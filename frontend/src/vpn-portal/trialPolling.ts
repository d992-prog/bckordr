const TRIAL_POLL_DELAY_MS = 2_000;
const TRIAL_POLL_LIMIT = 30;

type Schedule = (callback: () => void, delay: number) => number;

export function nextTrialPoll(
  completedPolls: number,
  poll: () => void,
  schedule: Schedule = window.setTimeout.bind(window),
): number | null {
  if (completedPolls >= TRIAL_POLL_LIMIT) {
    return null;
  }
  return schedule(poll, TRIAL_POLL_DELAY_MS);
}
