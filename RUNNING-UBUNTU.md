# Running on Ubuntu, terminal by terminal

Every command needed to bring this system up on Ubuntu 22.04, in the order you
type them, with what each terminal is for and how to tell it worked.

The system is **two repositories**:

| Repo | What it is | Where it runs |
|---|---|---|
| `bambu_monitor` (this one) | FastAPI backend + React dashboard | Anywhere, including Windows |
| `ar4Automating3DPrinter` | ROS 2 / MoveIt arm + camera automation | Linux only, and only if you have an arm |

The dashboard talks to the arm by importing the automation package in-process
(`--robot-mode ros`), so on the machine that drives the arm **both repos must
be present** and the dashboard must run in a shell that has ROS sourced. That
last point is the single most common way this fails to start.

Pick the scenario you actually need:

| Scenario | Terminals | Needs ROS | Needs an arm |
|---|---|---|---|
| [A — dashboard only](#scenario-a-dashboard-only-one-terminal) | 1 | no | no |
| [B — dashboard + fake arm](#scenario-b-dashboard-plus-a-fake-arm-one-terminal) | 1 | no | no |
| [C — dashboard + Gazebo](#scenario-c-dashboard-plus-gazebo-two-terminals) | 2 | yes | no |
| [D — dashboard + physical xArm 6](#scenario-d-dashboard-plus-the-physical-xarm-6-two-terminals) | 2 | yes | yes |
| [E — frontend development](#scenario-e-frontend-development-two-terminals) | 2 | no | no |

Scenarios A, B and E are verified. C and D are **not** — see
[What has actually been run](#what-has-actually-been-run) before trusting them.

---

## One-time setup

Skip the sections that don't apply to your scenario.

### 1. Both repositories, side by side

```bash
cd ~
git clone <bambu_monitor remote> bambu_monitor
git clone <ar4Automating3DPrinter remote> ar4Automating3DPrinter   # scenarios C and D only
```

The dashboard finds the automation repo through `AR4_AUTOMATION_REPO`, falling
back to `~/ar4Automating3DPrinter`. Clone it there and you never have to set
the variable.

### 2. Python and Node (every scenario)

Ubuntu 22.04 ships Python 3.10, which is also what ROS 2 Humble builds `rclpy`
against. **Use 3.10 for the virtualenv** — a 3.11 or 3.12 venv cannot import
ROS's compiled extension modules, and the failure only shows up when you try to
start the robot backend.

```bash
sudo apt install python3.10-venv python3-pip
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install nodejs

cd ~/bambu_monitor
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd frontend && npm install && npm run build && cd ..
```

`npm run build` writes `frontend/dist`, which the server serves. Rebuild it
after any frontend change, or use [scenario E](#scenario-e-frontend-development-two-terminals).

**Scenarios C and D also need:**

```bash
pip install -r requirements-robot.txt
```

That pins `numpy<2` and `opencv-python<4.12`, because ROS Humble's `cv_bridge`
is built against the NumPy 1.x C API and fails at import under NumPy 2. Do not
install it on a machine you also use for detector training — that stack wants
NumPy 2. This is exactly why they are separate files.

### 3. ROS 2 and the arm workspace (scenarios C and D)

Install ROS 2 Humble Desktop and MoveIt from the official instructions, then
build a workspace containing `xarm_ros2` (and `annin_ar4_*` if you are driving
an AR4 rather than an xArm):

```bash
sudo apt install ros-humble-desktop ros-humble-moveit \
                 ros-humble-realsense2-camera ros-humble-controller-manager

mkdir -p ~/dev_ws/src && cd ~/dev_ws/src
git clone https://github.com/xArm-Developer/xarm_ros2.git --recursive -b <branch>
cd ~/dev_ws
source /opt/ros/humble/setup.bash
colcon build
```

**Which branch:** the Lite 6 launch scripts in the automation repo say
`xarm_ros2` **jazzy** branch, on ROS 2 **Humble** — not the pairing you would
guess, and not something to silently "correct" to `-b humble`. Use whatever
those script comments call for, and if the arm's services or launch files do
not match, that mismatch is the first thing to check.

Verify which workspace is actually being sourced before you debug anything
else:

```bash
ros2 pkg prefix xarm_api
ros2 pkg prefix xarm_moveit_config
```

The launch scripts expect the workspace at `~/dev_ws`
(`scripts/launchPhysicalXArm6.sh`, overridable with `XARM_WS`) or `~/ar4_ws`
(the Lite 6 scripts). Use one workspace and point the scripts at it —
maintaining two sourced `xarm_ros2` installations is a reliable way to spend an
afternoon debugging a service that exists only in the other one.

### 4. Enable the xarm_api services (scenario D)

`xarm_api` only creates services that are explicitly listed in a parameter
file, and the ones this system needs are **off by default upstream**. Copy the
automation repo's version over and rebuild:

```bash
cp ~/ar4Automating3DPrinter/config/xarm_user_params.yaml \
   ~/dev_ws/src/xarm_ros2/xarm_api/config/xarm_user_params.yaml

cd ~/dev_ws
source /opt/ros/humble/setup.bash
colcon build --packages-select xarm_msgs xarm_api xarm_moveit_config
source install/setup.bash
```

That file enables the safety-profile services, the Lite 6 gripper services, and
`set_cgpio_digital` — the controller digital output the xArm 6's gripper hangs
off. Without it the gripper fails at `wait_for_service` with a message naming
this step. Details in the automation repo's
`docs/lite6_safety_commissioning.md`.

### 5. The printers

Nothing works until, on each printer:

1. **Settings → LAN-only Mode → ON**, then power-cycle.
2. **Settings → Developer Mode → ON.** It only appears once LAN-only is on.
3. Note the 8-character access code. It rotates on some firmware updates and is
   the most common cause of a connection that used to work.

Printers are registered **in the browser**, not on the command line. Full
details in [`CONNECTION.md`](CONNECTION.md).

### 6. A LAN password (only if you use `--lan`)

```bash
cd ~/bambu_monitor
echo 'a-shared-password' > .bambu-password
chmod 600 .bambu-password
```

`--lan` refuses to start without one rather than putting printer control on the
network unprotected. `.bambu-password` is gitignored.

---

## Scenario A: dashboard only (one terminal)

Real printers, no arm.

**Terminal 1 — the dashboard**

```bash
cd ~/bambu_monitor
source .venv/bin/activate
python -m server --lan
```

It prints the URLs to hand out:

```
  Bambu Monitor -- serving to the LAN on 0.0.0.0:8000
  password loaded from .bambu-password

  hand out one of these:
    http://192.168.1.42:8000
```

Open one in a browser and go to **Setup → Printers → Add a printer**.

For this machine only, with no password: `python -m server`.

The failure detector does **not** need its own terminal. The server spawns it
as a subprocess per camera-enabled printer and restarts it if it dies.

---

## Scenario B: dashboard plus a fake arm (one terminal)

No hardware, no ROS. This is the mode the Robot page is developed against, and
the only one that runs on a machine with no ROS at all.

```bash
cd ~/bambu_monitor
source .venv/bin/activate
python -m server --mock --robot-mode mock
```

`--mock` seeds three fake printers; `--robot-mode mock` adds the Robot page
with an in-memory arm, two fake ArUco markers, and a working
calibration/observation-pose workflow. Nothing moves and nothing is written to
the automation repo.

---

## Scenario C: dashboard plus Gazebo (two terminals)

### Terminal 1 — simulator and MoveIt

```bash
source /opt/ros/humble/setup.bash
source ~/dev_ws/install/setup.bash
cd ~/ar4Automating3DPrinter
./scripts/launchVirtualXArmLite6.sh     # Lite 6 in Gazebo
# or ./scripts/launchVirtualRobot.sh    # AR4 in Gazebo
```

Leave it running. **Wait until the Gazebo window has finished loading and the
controllers have spawned** before starting terminal 2 — the backend waits only
a couple of seconds for `/joint_states` and reports itself unavailable if the
simulator is still coming up.

### Terminal 2 — the dashboard

```bash
source /opt/ros/humble/setup.bash
source ~/dev_ws/install/setup.bash
cd ~/bambu_monitor
source .venv/bin/activate
python -m server --robot-mode ros --robot-sim --robot-type lite6
```

This terminal needs **both** ROS sourced and the venv active. The order does
not matter — sourcing ROS puts `rclpy` on `PYTHONPATH`, activating the venv
does not clear it, and neither changes which `python` runs. What matters is
that both happened, so check before starting the server:

```bash
python -c "import rclpy; print('rclpy ok')"
```

If that fails the server still starts, and the Robot page reports
`robot startup failed` — the backend is built on its worker thread, so a
missing `rclpy` surfaces on the page rather than as a crash at boot.

In sim the automation package disables the gripper (physics instability), and
teach mode and hand-eye calibration are refused as physical-only.

---

## Scenario D: dashboard plus the physical xArm 6 (two terminals)

> Read the automation repo's `docs/lite6_safety_commissioning.md` first. The
> software checks here supplement the controller, MoveIt collision checking,
> guarding and the physical emergency stop. They are not a safety-rated control
> system. Keep one person at the e-stop.

### Terminal 1 — arm, camera, MoveIt

```bash
source /opt/ros/humble/setup.bash
source ~/dev_ws/install/setup.bash
cd ~/ar4Automating3DPrinter
export ROBOT_IP=192.168.1.150          # your control box; ping it first
./scripts/launchPhysicalXArm6.sh
```

The script starts the RealSense driver in the background and MoveIt in the
foreground, so this is one terminal, not two. It needs `ROBOT_IP` and will
refuse to start without it.

Before moving on, confirm the pieces the backend depends on exist:

```bash
ros2 topic echo /joint_states --once
ros2 service type /xarm/set_cgpio_digital      # the gripper output
ros2 topic echo /camera/color/camera_info --once
ros2 action list -t | grep -E 'move_action|execute_trajectory'
```

**Check the gripper output and polarity once, with the jaws empty and the arm
clear.** `ionum` is the CO number the gripper is wired to (CO0–CO7 on the
control box), and `value: 1` must *grip*:

```bash
ros2 service call /xarm/set_cgpio_digital xarm_msgs/srv/SetDigitalIO "{ionum: 0, value: 1}"
ros2 service call /xarm/set_cgpio_digital xarm_msgs/srv/SetDigitalIO "{ionum: 0, value: 0}"
```

If nothing moves, try the other CO numbers until one does, then set that in
the Gripper card (or `XARM_GRIPPER_OUTPUT`). If the jaws move the *wrong way*,
swap `closed_value` and `open_value` in the automation repo's
`ar4_automation/robot_config.py`. Do not edit the driver for either.

### Terminal 2 — the dashboard

```bash
source /opt/ros/humble/setup.bash
source ~/dev_ws/install/setup.bash
cd ~/bambu_monitor
source .venv/bin/activate
export AR4_AUTOMATION_REPO=~/ar4Automating3DPrinter   # only if not the default
python -m server --lan --robot-mode ros --robot-type xarm6
```

On startup the backend applies the UFACTORY safety profile (collision
sensitivity, reduced mode, TCP and joint speed caps) and activates the
trajectory controller. If any of that fails it says so and **motion stays
blocked** — the Robot page shows which interlock is red rather than letting you
drive an arm in an unknown state.

### In the browser

Open the URL, go to the **Robot** page (Control group), and work down it:

1. Check the **safety preflight** block — every row green.
2. Tick **"Enable movement controls — I have verified the workspace is clear."**
   Nothing moves until you do.
3. Start with **one 5 mm jog**, then `home`, then a single joint goal.
4. Gripper open/close. The card shows *last commanded*, not a sensed position:
   this gripper has no feedback line.
5. Only then ArUco scanning, and only then pick/place.

For hand-eye calibration, teach mode and observation poses, see
[`master.md` §16](master.md).

---

## Scenario E: frontend development (two terminals)

Vite's dev server gives hot reload and proxies `/api` and `/ws` to the backend,
so you don't rebuild on every edit.

**Terminal 1 — backend on port 8000** (the proxy target is hardcoded):

```bash
cd ~/bambu_monitor
source .venv/bin/activate
python -m server --mock --robot-mode mock
```

**Terminal 2 — Vite**

```bash
cd ~/bambu_monitor/frontend
npm run dev
```

Open the URL Vite prints (5173 by default), **not** 8000. When you are done,
`npm run build` so the backend serves the change too.

---

## Environment variables

| Variable | Read by | Default | Notes |
|---|---|---|---|
| `BAMBU_PASSWORD` | dashboard | — | Always wins over `.bambu-password` |
| `AR4_AUTOMATION_REPO` | dashboard | `~/ar4Automating3DPrinter` | Same as `--robot-repo` |
| `XARM_CAMERA_MODE` | dashboard | `webcam` | `webcam` with no index degrades to `disabled`, which means "use the RealSense ROS topics" |
| `XARM_CAMERA_INDEX` | dashboard | — | `/dev/videoN` number, for a USB camera instead of the RealSense |
| `XARM_GRIPPER_OUTPUT` | dashboard | whatever `robot_config.py` says | Which controller output drives the gripper, `0`–`7` (CO0–CO7). `--robot-mode ros` only; the Robot page can change it at runtime either way. A bad value refuses to start rather than driving the wrong pin |
| `ROBOT_IP` | launch script | — | Required; the control box's IP |
| `XARM_WS` | launch script | `~/dev_ws` | Where `xarm_ros2` is built |

Leave the two `XARM_CAMERA_*` variables unset for a RealSense on the wrist.
Set both to use a USB webcam. The Robot page can also switch cameras at
runtime without a restart.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Robot page says `robot startup failed: No module named 'rclpy'` | The dashboard's shell has no ROS sourced, or the venv is not Python 3.10 | Source ROS in *that* terminal; check with `python -c "import rclpy"` |
| `numpy.core.multiarray failed to import` on robot start | NumPy 2 with ROS Humble's `cv_bridge` | `pip install -r requirements-robot.txt` in the venv |
| Robot page loads but `available: false` | Terminal 1 was not up, or not up *yet*, when the server started | Start MoveIt first, wait for controllers, restart the server |
| Safety preflight shows `trajectory_controller` red | Expected **in teach mode** — it hands ownership back from MoveIt | Press *Return to MoveIt* |
| Safety preflight red otherwise | Stale `/joint_states`, a controller error, or the profile failed to apply | Read the `detail` on the red row; check terminal 1 |
| Gripper command fails at `wait_for_service` | `set_cgpio_digital` not enabled in `xarm_user_params.yaml` | Redo [setup step 4](#4-enable-the-xarm_api-services-scenario-d) |
| Gripper opens when you press close | Inverted wiring | Swap `closed_value`/`open_value` in the automation repo's `robot_config.py` |
| Nothing happens on open/close, no error | The gripper is on a different CO than the one selected | Pick the right one in the Gripper card, or set `XARM_GRIPPER_OUTPUT` |
| "open the gripper before changing its output" | You tried to re-point the output mid-grip | Open it first — the old pin stays latched otherwise |
| Pick/place refused: "requires a configured physical gripper" | `'gripper': None` for that robot | Configure the tool in the automation repo's `robot_config.py` |
| `--lan` refuses to start | No password | Create `.bambu-password` |
| Dashboard loads but has no styling / old UI | `frontend/dist` is stale or missing | `cd frontend && npm run build` |
| Gazebo hangs on launch | A stale `gz` server from a previous run | The Lite 6 sim script kills them; otherwise `pkill -9 -f gz-sim` |
| Camera picker is empty | Not on Linux, or no V4L2 devices | It reads `/sys/class/video4linux`; `[]` is not an error |

---

## Shutting down

Ctrl-C each terminal, **dashboard first**. Stopping the ROS side while the
dashboard still holds a live node leaves it retrying against a bus that is
going away.

Ctrl-C in terminal 1 tears down MoveIt and the camera driver together — the
launch scripts trap the signal and kill the background camera process.

Ctrl-C does **not** release a latched gripper. The CO output survives the
process, so open the gripper from the Robot page before shutting down if it is
holding a plate.

---

## Checking your install

No hardware needed for either:

```bash
cd ~/bambu_monitor
source .venv/bin/activate
python -m pytest -q

cd frontend && npm test
```

---

## What has actually been run

Following the rule in [`master.md`](master.md) §1.1 that "verified" has to say
*on what*:

- **Scenarios A, B and E are verified**, including the whole Robot page against
  `--robot-mode mock`.
- **Scenarios C and D have never been executed since the arm was ported onto
  this dashboard.** No machine in this project has both ROS and the arm
  attached. The commands above are what the code and the launch scripts say
  they need, not a transcript of a session that worked. Expect to debug the
  first bring-up, and do it with the e-stop in reach.

[`master.md` §16](master.md) marks exactly which robot claims are hardware
claims and which are code claims.
