---
name: servo-tracking
description: Use ONLY to follow/track/watch a movable physical OBJECT vision can recognize (cup, bottle, phone, hand, person, pen, book, remote, toy, keys, pet, a specific face). NEVER use for furniture or fixed locations (desk, table, wall, floor, ceiling, door, window, workspace, room) — those are directions; use servo-control /servo/aim. NEVER use for direction words (left, right, up, down, center). If the user combines a direction AND an object ("look at the desk and follow the cup", "point at the table and track the phone"), fire TWO markers in order — servo-control /servo/aim first, then /servo/track here. The verb "look at" is ambiguous — if what follows is furniture/location, route to servo-control instead.
---

# Vision Tracking

## Quick Start
Tracks and follows any object by name. YOLOWorld detects the object in the camera frame, TrackerVit follows it in real-time with servo movement.

## Workflow
1. User names an object to track.
2. **If the user also hints at a direction** ("look at desk and follow cup", "point at the table and watch the phone"), fire `/servo/aim` FIRST so the camera is pointing at the right region before YOLO detection runs. Then fire `/servo/track`. The aim should complete in ~2s before the track call takes over.
3. Prefix reply with `[HW:/servo/track:{"target":["<label1>","<label2>"]}]` — Lamp detects and follows.
   - `target` accepts a list of candidate labels. Pass 2-4 synonyms/variants to maximise the chance YOLOWorld finds the object on the first try.
   - A single string also works (`{"target":"cup"}`) for backward compatibility, but the list form is preferred when the object could reasonably have multiple names.
4. To stop, prefix with `[HW:/servo/track/stop:{}] (POST)`.

## Examples

**Input:** "Look at the desk and follow the cup"
**Output:** `[HW:/servo/aim:{"direction":"desk"}][HW:/servo/track:{"target":["cup","mug","coffee cup"]}]` Looking at the desk and locking onto the cup.

**Input:** "Point at the table and track my phone"
**Output:** `[HW:/servo/aim:{"direction":"desk"}][HW:/servo/track:{"target":["phone","smartphone","mobile phone"]}]` Aimed at the table, tracking your phone.

**Input:** "Follow the cup"
**Output:** `[HW:/servo/track:{"target":["cup","mug","coffee cup"]}]` OK, following the cup!

**Input:** "Look at the bottle"
**Output:** `[HW:/servo/track:{"target":["bottle","water bottle","plastic bottle"]}]` Watching the bottle.

**Input:** "Track that person"
**Output:** `[HW:/servo/track:{"target":["person","man","woman"]}]` Following them now.

**Input:** "Watch my phone"
**Output:** `[HW:/servo/track:{"target":["phone","smartphone","mobile phone"]}]` Got it, tracking your phone.

**Input:** "Follow the teddy bear"
**Output:** `[HW:/servo/track:{"target":["teddy bear","stuffed animal","plush toy"]}]` Tracking the teddy bear!

**Input:** "Stop following" / "Stop tracking"
**Output:** `[HW:/servo/track/stop:{}] (POST)` Stopped tracking.

**Input:** "What can you track?"
**Output:** I can track most common objects — cups, bottles, phones, laptops, books, people, bags, and more. Just tell me what to follow!

## How to Control Tracking

**No exec/curl needed.** Inline markers at start of reply:

```
[HW:/servo/track:{"target":["cup","mug","coffee cup"]}] Following the cup.
[HW:/servo/track:{"target":["person"]}] Tracking you now.
[HW:/servo/track/stop:{}] (POST) Stopped tracking.
```

### Target names

Prefer a **list of 2-4 candidate labels** in English. YOLOWorld evaluates all candidates and picks the highest-confidence detection across the set — synonyms increase the chance of a successful first-try detection when the user's wording doesn't exactly match COCO/training vocabulary.

Common objects: person, cup, bottle, glass, phone, laptop, keyboard, mouse, book, pen, notebook, bag, chair, monitor, remote control, plate, bowl, plant, vase, clock, lamp, speaker, headphones, watch, glasses, hat, shoe, toy, ball, teddy bear.

Any label works (open-vocabulary detection). Don't include too many unrelated items in the list — that risks matching a nearby but wrong object.

### How it works internally
1. Camera captures current frame
2. YOLOWorld API evaluates all candidate labels (~1-2s)
3. Highest-confidence detection becomes the initial bbox
4. TrackerVit locks on and follows in a move-then-freeze loop (~7 FPS)
5. Servo base_yaw + base_pitch nudges to keep object centered
6. Auto-stops when object is lost, out of range, or after 5 minutes

## Error Handling
- If the object is not found: "I can't see a {target} right now. Try pointing me toward it, or try a different name."
- If tracking stops unexpectedly: "I lost the {target}. Want me to try again?"
- If servo is not available: "Servo is not available right now."

## Rules
- Only one object can be tracked at a time. Starting a new track stops the previous one.
- Tracking auto-stops when the object leaves the frame, gets occluded, or after 5 minutes.
- When tracking stops, the servo holds its last position (no snap back to idle).
- Do NOT use this for emotional reactions — use the Emotion skill instead.
- Prefer `target` as a list of 2-4 English synonyms for better first-try detection. Avoid packing unrelated labels into the list.
