# Task parent

- status: `running` (execution_right_claimed)
- run: `run-parent`; namespace `project-a`, session `s1`
- root: `parent`; parent: `—`; depth 0
- attempts: 1; current attempt: `parent-a1`
- waiting for: `—`
- workspace: child-0000 (`sha256:1026c8adade329331856833fa48f72b1af2d6b589dd1ac87013611ba7a983f15`)

## Scheduling claim

holder `worker-1`, attempt `parent-a1`, fence 1, live (expires 1792143134.879159).

## Inputs

| seq | input | kind | state | digest | applied rev |
| --- | --- | --- | --- | --- | --- |
| 0 | `initial` | initial | applied | sha256:50b680ae893d… | 1 |
| 1 | `steer-1` | steering | applied | sha256:c985e397b371… | 1 |

## Children

| child | spawn | relation | child status | outcome | accepted |
| --- | --- | --- | --- | --- | --- |
| `child` | `spawn:0001…` | completed | completed | completed | True |

## Budget

| reservation | unit | amount | state | settled |
| --- | --- | --- | --- | --- |
| `grant:spawn:0001` | model_requests | 2.0 | settled | 1.0 |

## Notifications (2 pending)

| notice | kind | delivered | attempts | payload |
| --- | --- | --- | --- | --- |
| `child_spawned:spawn:0001` | child_spawned | no | 0 | sha256:4dffd2cbb4ab… |
| `child_completed:child` | child_completed | no | 0 | sha256:b0dc327072eb… |
