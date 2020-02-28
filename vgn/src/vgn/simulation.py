import time

import numpy as np
import pybullet
import scipy.stats as stats

from vgn.grasp import Label
from vgn.perception.camera import PinholeCameraIntrinsic
from vgn.utils import btsim
from vgn.utils.transform import Rotation, Transform


class GraspExperiment(object):
    """Simulation of a grasping experiment.

    In this simulation, world, task and robot base frames are identical.
    """

    def __init__(self, urdf_root, object_set, hand, size, gui=True, rtf=-1.0):
        self.urdf_root = urdf_root
        self.size = size
        self.gui = gui
        self.wait = False

        self.world = btsim.BtWorld(gui, rtf)
        self.robot = Robot(self.world, hand)

        self.object_set = {
            "debug": DebugObjectSet,
            "cuboid": CuboidObjectSet,
            "kappler": KapplerObjectSet,
            "ycb": YcbObjectSet,
            "urdf_zoo": UrdfZooObjectSet,
            "adversarial": AdversarialObjectSet,
        }[object_set](self)

    def setup(self):
        self.world.reset()
        self.world.set_gravity([0.0, 0.0, -9.81])

        if self.gui:
            self._draw_task_space()

        # Load support surface
        plane = self.world.load_urdf(self.urdf_root / "plane/plane.urdf")
        plane.set_pose(Transform(Rotation.identity(), [0.0, 0.0, 0.03]))

        # Load robot
        pose = Transform(Rotation.identity(), np.r_[0.0, 0.0, 1.0])
        self.robot.reset(pose)
        self.robot.move_gripper(1.0)

        # Load camera
        intrinsic = PinholeCameraIntrinsic(640, 480, 540.0, 540.0, 320.0, 240.0)
        self.camera = self.world.add_camera(intrinsic, 0.1, 2.0)

        # Load objects
        self.object_set.spawn()

    def pause(self):
        self.world.pause()

    def resume(self):
        self.world.resume()

    def save_state(self):
        self.snapshot_id = self.world.save_state()

    def restore_state(self):
        self.world.restore_state(self.snapshot_id)

    def spawn_object(self, urdf_path, pose, scale=1.0):
        body = self.world.load_urdf(urdf_path, scale=scale)
        body.set_pose(pose)
        for _ in range(240):
            self.world.step()

    def test_grasp(self, grasp_pose):
        """Open-loop grasp execution.
        
        Args:
            grasp_pose: The grasp pose w.r.t. to the robot base frame.

        Return:
            A tuple (label, width) with the grasp outcome and grasp width.
        """
        pregrasp_pose = grasp_pose * Transform(Rotation.identity(), [0.0, 0.0, -0.05])
        retrieve_pose = grasp_pose * Transform(Rotation.identity(), [0.0, 0.0, -0.1])

        if not self._move_to_pregrasp_pose(pregrasp_pose):
            return Label.COLLISION, 0.0

        if not self._move_to_grasp_pose(grasp_pose):
            return Label.COLLISION, 0.0

        if not self._close_hand():
            return Label.SLIPPED, 0.0

        if not self._retrieve_object(retrieve_pose):
            return Label.SLIPPED, 0.0

        width = self.robot.read_gripper() * self.robot.max_gripper_width
        return Label.SUCCESS, width

    def _move_to_pregrasp_pose(self, pregrasp_pose):
        return self.robot.set_tcp(pregrasp_pose)

    def _move_to_grasp_pose(self, grasp_pose):
        return self.robot.move_tcp_xyz(grasp_pose)

    def _close_hand(self):
        self.robot.move_gripper(0.0)
        return self._check_grasp()

    def _retrieve_object(self, pregrasp_pose):
        self.robot.move_tcp_xyz(pregrasp_pose)
        return self._check_grasp()

    def _check_grasp(self, threshold=0.2):
        return self.robot.read_gripper() > threshold

    def _draw_task_space(self):
        lines = [
            [[0.0, 0.0, 0.0], [self.size, 0.0, 0.0]],
            [[self.size, 0.0, 0.0], [self.size, self.size, 0.0]],
            [[self.size, self.size, 0.0], [0.0, self.size, 0.0]],
            [[0.0, self.size, 0.0], [0.0, 0.0, 0.0]],
            [[0.0, 0.0, self.size], [self.size, 0.0, self.size]],
            [[self.size, 0.0, self.size], [self.size, self.size, self.size]],
            [[self.size, self.size, self.size], [0.0, self.size, self.size]],
            [[0.0, self.size, self.size], [0.0, 0.0, self.size]],
            [[0.0, 0.0, 0.0], [0.0, 0.0, self.size]],
            [[self.size, 0.0, 0.0], [self.size, 0.0, self.size]],
            [[self.size, self.size, 0.0], [self.size, self.size, self.size]],
            [[0.0, self.size, 0.0], [0.0, self.size, self.size]],
        ]

        for line in lines:
            self.world.p.addUserDebugLine(
                lineFromXYZ=line[0], lineToXYZ=line[1], lineColorRGB=[0.5, 0.5, 0.5],
            )


class Robot(object):
    """Simulated robot arm with a simple parallel-jaw gripper."""

    def __init__(self, world, hand):
        self.world = world
        self.urdf_path = hand.urdf_path

        self.T_tool0_tcp = hand.T_tool0_tcp
        self.T_tcp_tool0 = self.T_tool0_tcp.inverse()
        self.max_gripper_width = hand.max_gripper_width

    def reset(self, pose):
        self.body = self.world.load_urdf(str(self.urdf_path))
        self.body.set_pose(pose)
        self.constraint = self.world.add_constraint(
            self.body,
            None,
            None,
            None,
            pybullet.JOINT_FIXED,
            [0.0, 0.0, 0.0],
            Transform.identity(),
            pose,
        )

    def set_tcp(self, pose):
        T_world_tool0 = pose * self.T_tcp_tool0
        self.body.set_pose(T_world_tool0)
        self.constraint.change(T_world_tool0, max_force=300)
        self.world.step()
        return not self.world.check_collisions(self.body)

    def move_tcp_xyz(
        self, target_pose, eef_step=0.002, vel=0.10, collision_threshold=100
    ):
        pose = self.body.get_pose() * self.T_tool0_tcp

        pos_diff = target_pose.translation - pose.translation
        n_steps = int(np.linalg.norm(pos_diff) / eef_step)
        dist_step = pos_diff / n_steps
        dur_step = np.linalg.norm(dist_step) / vel

        for _ in range(n_steps):
            pose.translation += dist_step
            self.constraint.change(pose * self.T_tcp_tool0, max_force=300)
            for _ in range(int(dur_step / self.world.dt)):
                self.world.step()

            for collision in self.world.check_collisions(self.body):
                if collision.force > collision_threshold:
                    return False

        return True

    def read_gripper(self):
        pos_l = self.body.joints["finger_l"].get_position()
        pos_r = self.body.joints["finger_r"].get_position()
        width = pos_l + pos_r
        return width / self.max_gripper_width

    def move_gripper(self, width):
        width *= 0.5 * self.max_gripper_width
        self.body.joints["finger_l"].set_position(width)
        self.body.joints["finger_r"].set_position(width)
        for _ in range(int(0.5 / self.world.dt)):
            self.world.step()


class ObjectSet(object):
    def __init__(self, sim):
        self.sim = sim
        self.urdf_root = sim.urdf_root
        self.size = sim.size

    def _discover_urdfs(self, root):
        urdfs = [d / (d.name + ".urdf") for d in root.iterdir() if d.is_dir()]
        return urdfs

    def _sample_num_objects(self):
        expected_num_of_objects = 3
        num_objects = np.random.poisson(expected_num_of_objects - 1) + 1
        return num_objects

    def _sample_pose(self):
        l, u = 0.0, self.size
        mu, sigma = self.size / 2.0, self.size / 4.0
        X = stats.truncnorm((l - mu) / sigma, (u - mu) / sigma, loc=mu, scale=sigma)
        position = np.r_[X.rvs(2), 0.15]
        orientation = Rotation.random()
        return Transform(orientation, position)


class DebugObjectSet(ObjectSet):
    def __init__(self, sim):
        super().__init__(sim)

    def spawn(self):
        urdf_path = self.urdf_root / "toy_blocks/cuboid/cuboid.urdf"
        position = np.r_[0.5 * self.size, 0.5 * self.size, 0.15]
        orientation = Rotation.identity()
        self.sim.spawn_object(urdf_path, Transform(orientation, position))


class CuboidObjectSet(ObjectSet):
    def __init__(self, sim):
        super().__init__(sim)

    def spawn(self):
        urdf_path = self.urdf_root / "toy_blocks/cuboid/cuboid.urdf"
        pose = self._sample_pose()
        self.sim.spawn_object(urdf_path, pose)


class KapplerObjectSet(ObjectSet):
    def __init__(self, sim):
        super().__init__(sim)
        self.urdfs = self._discover_urdfs(self.urdf_root / "kappler")

    def spawn(self):
        num_objects = self._sample_num_objects()
        for urdf_path in np.random.choice(self.urdfs, size=num_objects):
            pose = self._sample_pose()
            scale = np.random.uniform(0.8, 1.0)
            self.sim.spawn_object(urdf_path, pose, scale)


class YcbObjectSet(ObjectSet):
    def __init__(self, sim):
        super().__init__(sim)
        self.urdfs = self._discover_urdfs(self.urdf_root / "ycb")

    def spawn(self):
        num_objects = self._sample_num_objects()
        for urdf_path in np.random.choice(self.urdfs, size=num_objects):
            pose = self._sample_pose()
            scale = np.random.uniform(0.8, 1.0)
            self.sim.spawn_object(urdf_path, pose, scale)


class UrdfZooObjectSet(ObjectSet):
    """Combination of objects from Kappler and YCB."""

    def __init__(self, sim):
        super().__init__(sim)
        kappler_urdfs = self._discover_urdfs(self.urdf_root / "kappler")
        ycb_urdfs = self._discover_urdfs(self.urdf_root / "ycb")
        self.urdfs = kappler_urdfs + 10 * ycb_urdfs

    def spawn(self):
        num_objects = self._sample_num_objects()
        for urdf_path in np.random.choice(self.urdfs, size=num_objects):
            pose = self._sample_pose()
            scale = np.random.uniform(0.8, 1.0)
            self.sim.spawn_object(urdf_path, pose, scale)


class AdversarialObjectSet(ObjectSet):
    def __init__(self, sim):
        super().__init__(sim)
        self.urdfs = self._discover_urdfs(self.urdf_root / "adversarial")

    def spawn(self):
        urdf_path = np.random.choice(self.urdfs)
        pose = self._sample_pose()
        self.sim.spawn_object(urdf_path, pose)

