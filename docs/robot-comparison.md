# What each robot can do

Every skill runs on every robot whose hardware declares what it needs — so a skill written for Lamp works on a Reachy Mini, and a skill you write today works on the robot you port tomorrow.

Skills install by capability: a robot's [`ROBOT.md`](../devices/lamp/ROBOT.md) declares the hardware it has, and every skill that needs only that hardware lands on it. Nothing is per-model, so the grid fills itself in as bodies arrive.

| PHYSICAL SKILLS | <a href="../devices/lamp"><img src="../devices/lamp/images/lamp-white.webp" width="150" alt="Lamp"><br>Lamp</a> | <a href="../devices/intern-v2"><img src="../devices/intern-v2/images/intern-tile.webp" width="150" alt="Intern"><br>Intern</a> | <a href="../devices/reachy-mini"><img src="../devices/reachy-mini/images/reachy-mini.webp" width="150" alt="Reachy Mini"><br>Reachy Mini</a> | <a href="../devices/unitree-go2w"><img src="../devices/unitree-go2w/images/go2-w-tile.webp" width="150" alt="Go2-W"><br>Go2-W</a> |
|---|:---:|:---:|:---:|:---:|
| [camera](../skills/camera/)<br>See the room | ✅ |  | ✅ | ○ |
| [servo-tracking](../skills/servo-tracking/)<br>Track an object | ✅ |  | ○ | ○ |
| [face-enroll](../skills/face-enroll/)<br>Know your face | ✅ |  | ✅ |  |
| [speaker-recognizer](../skills/speaker-recognizer/)<br>Know who is speaking | ✅ | ✅ | ✅ | ○ |
| [voice](../skills/voice/)<br>Talk back | ✅ | ✅ | ✅ | ○ |
| [audio](../skills/audio/)<br>Sound and volume | ✅ | ✅ | ✅ | ○ |
| [servo-control](../skills/servo-control/)<br>Move and gesture | ✅ |  | ✅ | ○ |
| [emotion](../skills/emotion/)<br>Show emotion | ✅ |  | ✅ |  |
| [led-control](../skills/led-control/)<br>Colors and effects | ✅ | ✅ |  |  |
| [scene](../skills/scene/)<br>Six lighting scenes | ✅ |  |  |  |
| [sensing](../skills/sensing/)<br>Sense the room | ✅ | ✅ | ✅ | ○ |
| [sensing-track](../skills/sensing-track/)<br>Remember what it sensed | ✅ | ✅ | ✅ | ○ |
| [guard](../skills/guard/)<br>Guard the house | ✅ |  | ✅ |  |
| [music](../skills/music/)<br>Play music | ✅ | ✅ | ✅ |  |
| [music-suggestion](../skills/music-suggestion/)<br>Suggest a song | ✅ | ✅ | ✅ |  |
| [emotion-detection](../skills/user-emotion-detection/)<br>Read your mood | ✅ | ✅ | ✅ | ○ |
| [mood](../skills/mood/)<br>Track how you feel | ✅ | ✅ | ✅ | ○ |
| [wellbeing](../skills/wellbeing/)<br>Posture and breaks | ✅ | ✅ | ✅ | ○ |
| [habit](../skills/habit/)<br>Learn your routines | ✅ | ✅ | ✅ | ○ |

| DIGITAL SKILLS | <a href="../devices/lamp"><img src="../devices/lamp/images/lamp-white.webp" width="150" alt="Lamp"><br>Lamp</a> | <a href="../devices/intern-v2"><img src="../devices/intern-v2/images/intern-tile.webp" width="150" alt="Intern"><br>Intern</a> | <a href="../devices/reachy-mini"><img src="../devices/reachy-mini/images/reachy-mini.webp" width="150" alt="Reachy Mini"><br>Reachy Mini</a> | <a href="../devices/unitree-go2w"><img src="../devices/unitree-go2w/images/go2-w-tile.webp" width="150" alt="Go2-W"><br>Go2-W</a> |
|---|:---:|:---:|:---:|:---:|
| [Gmail](../skills/connectors/)<br>Manage your email | ✅ | ✅ | ✅ | ○ |
| [Calendar](../skills/connectors/)<br>Book and move meetings | ✅ | ✅ | ✅ | ○ |
| [Notion](../skills/connectors/)<br>Search and write your notes | ✅ | ✅ | ✅ | ○ |
| [GitHub](../skills/connectors/)<br>Issues and pull requests | ✅ | ✅ | ✅ | ○ |
| [Your Mac](../skills/computer-use/)<br>Drive apps and the browser | ✅ | ✅ | ✅ |  |
| [Claude Buddy](../skills/claude-buddy/)<br>Approve prompts by voice | ✅ | ✅ | ✅ | ○ |
| [Skill Creator](../skills/skill-creator/)<br>Write its own skills | ✅ | ✅ | ✅ | ○ |

✅ runs today · ○ on the way

Per-body notes — why a cell is blank or ○ — are in [`skills/README.md`](../skills/README.md#per-body-notes). The full catalog of 25 skills: [`skills/`](../skills/).
