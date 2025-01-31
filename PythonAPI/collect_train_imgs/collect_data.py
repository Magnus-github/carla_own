import glob
import os
import sys

try:
    sys.path.append(glob.glob('../carla/dist/carla-*%d.%d-%s.egg' % (
        sys.version_info.major,
        sys.version_info.minor,
        'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass


# ==============================================================================
# -- imports -------------------------------------------------------------------
# ==============================================================================

import carla
import logging

import weakref
import random
import math
import time
import queue
import cv2
import json
import csv

try:
    import pygame
    from pygame.locals import K_ESCAPE
    from pygame.locals import K_SPACE
    from pygame.locals import K_a
    from pygame.locals import K_d
    from pygame.locals import K_s
    from pygame.locals import K_w
except ImportError:
    raise RuntimeError('cannot import pygame, make sure pygame package is installed')

try: 
    import numpy as np
except ImportError:
    raise RuntimeError('cannot import numpy, make sure numpy package is installed')


SpawnActor = carla.command.SpawnActor


OUTPUT_FOLDER = "_dataset_vehicles_testing" 


if not os.path.exists(os.path.join("./",OUTPUT_FOLDER)):
    os.makedirs(os.path.join("./",OUTPUT_FOLDER))



# =============================================================================
# -- get bounding boxes -------------------------------------------------------
# =============================================================================

class GTBoundingBoxes(object):
    
    @staticmethod
    def get_bounding_boxes(world, vehicle, camera, K, labels, img=None):
        bounding_boxes = []

        for label in labels:
            bbs = list(world.get_level_bbs(label))
            for bb in bbs:
                bb_verts = GTBoundingBoxes.__filter_bbs(bb, vehicle, camera, K)
                bounding_boxes.append(bb_verts)

                if img is not None and bb_verts:
                    bb_verts.append(label)
                    img = cv2.line(img, (int(bb_verts[0]),int(bb_verts[1])), (int(bb_verts[2]),int(bb_verts[1])), (0,0,255, 255), 1)
                    img = cv2.line(img, (int(bb_verts[0]),int(bb_verts[3])), (int(bb_verts[2]),int(bb_verts[3])), (0,0,255, 255), 1)
                    img = cv2.line(img, (int(bb_verts[0]),int(bb_verts[1])), (int(bb_verts[0]),int(bb_verts[3])), (0,0,255, 255), 1)
                    img = cv2.line(img, (int(bb_verts[2]),int(bb_verts[1])), (int(bb_verts[2]),int(bb_verts[3])), (0,0,255, 255), 1)
        
        if img is not None:
            return bounding_boxes, img
        
        return bounding_boxes

    @staticmethod
    def __filter_bbs(bb, ego_vehicle, camera, K):
        bbox_verts = []

        # Get the camera matrix 
        world_2_camera = np.array(camera.get_transform().get_inverse_matrix())
        image_w = int(camera.attributes["image_size_x"])
        image_h = int(camera.attributes["image_size_y"])

        dist = bb.location.distance(ego_vehicle.get_transform().location)

        # Filter for the vehicles within 50m
        if dist < 50:
            # Calculate the dot product between the forward vector
            # of the vehicle and the vector between the vehicle
            # and the other vehicle. We threshold this dot product
            # to limit to drawing bounding boxes IN FRONT OF THE CAMERA
            forward_vec = ego_vehicle.get_transform().get_forward_vector()
            ray = bb.location - ego_vehicle.get_transform().location

            if forward_vec.dot(ray) > 1:
                verts = [v for v in bb.get_world_vertices(carla.Transform())]

                x_max = -10000
                x_min = 10000
                y_max = -10000
                y_min = 10000

                for vert in verts:
                    p = GTBoundingBoxes.__get_image_point(vert, K, world_2_camera)
                    # Find the rightmost vertex
                    if p[0] > x_max:
                        x_max = p[0]
                    # Find the leftmost vertex
                    if p[0] < x_min:
                        x_min = p[0]
                    # Find the highest vertex
                    if p[1] > y_max:
                        y_max = p[1]
                    # Find the lowest  vertex
                    if p[1] < y_min:
                        y_min = p[1]

                    # make sure bb is in image!
                    if x_min > 0 and x_max < image_w and y_min > 0 and y_max < image_h:
                        bbox_verts = [x_min, y_min, x_max, y_max]

        return bbox_verts
                
    @staticmethod
    def __get_image_point(loc, K, w2c):
        """
        Calculate 2D projecbb_vertstion of 3D coordinate.

        Input:
            loc: 3D coordinate of object (carla.Position object)
            K: camera projection matrix
            w2c: transformation matrix from point in worl to point in camera coord. system
        Output:
            2D point in image. 
        """

        # Format the input coordinate
        point = np.array([loc.x, loc.y, loc.z, 1])
        # transform to camera coordinates
        point_camera = np.dot(w2c, point)

        # New we must change from UE4's coordinate system to an "standard"
        # (x, y ,z) -> (y, -z, x)
        # and we remove the fourth component also
        point_camera = [point_camera[1], -point_camera[2], point_camera[0]]

        # now project 3D->2D using the camera matrix
        point_img = np.dot(K, point_camera)
        # normalize
        point_img[0] /= point_img[2]
        point_img[1] /= point_img[2]

        return point_img[0:2]
    

# =============================================================================
# -- static attack client -----------------------------------------------------
# =============================================================================


class StaticAttackScenario(object):
    def __init__(self) -> None:
        self.client = None
        self.world = None
        self.bp_lib = None
        self.vehicle_list = []
        self.pedestrian_list = []
        self.static_attacks = []

        self.car = None
        self.camera = None
        self.K = None
        self.image_queue = queue.LifoQueue(maxsize=10)

        self.ground_truth_annotations = {
            "info": {},

            "licenses": {},

            "images": [],

            "categories": [
                {"supercategory": "vehicle", "id": 1, "name": "car" },
                {"supercategory": "vehicle", "id": 2, "name": "truck" },
                {"supercategory": "vehicle", "id": 3, "name": "motorcycle" },
                {"supercategory": "vehicle", "id": 4, "name": "bicycle" },
                {"supercategory": "vehicle", "id": 5, "name": "bus" },
                {"supercategory": "vehicle", "id": 6, "name": "rider" },
                {"supercategory": "vehicle", "id": 7, "name": "train" },
                {"supercategory": "", "id": 8, "name": "pedestrian" }
            ],

            "annotations": []
        }

        self.objectlabel2categoryid = {
            carla.CityObjectLabel.Car           :   1,
            carla.CityObjectLabel.Truck         :   2,
            carla.CityObjectLabel.Motorcycle    :   3,
            carla.CityObjectLabel.Bicycle       :   4,
            carla.CityObjectLabel.Rider         :   5,
            carla.CityObjectLabel.Bus           :   6,
            carla.CityObjectLabel.Train         :   7,
            carla.CityObjectLabel.Pedestrians   :   8
        }

        self.display = None
        self.image = None
        self.capture = True

        # Get Walker spawn points
        self.walker_spawn_point = []
        with open("./../attack_scenario/pedestrian_spawn_points.csv", 'r') as file:
            csvreader = csv.reader(file)
            for i, row in enumerate(csvreader):
                if i==0:
                    continue
                spawn_point = carla.Transform()
                spawn_point.location.x = float(row[1])
                spawn_point.location.y = float(row[2])
                spawn_point.location.z = float(row[3])
                self.walker_spawn_point.append(spawn_point)

    def set_synchronous_mode(self, mode):
        # Set up the simulator in synchronous mode
        settings = self.world.get_settings()
        settings.synchronous_mode = mode # Enables/disables synchronous mode
        settings.fixed_delta_seconds = 0.05
        self.world.apply_settings(settings)

    def spawn_npc_vehicles(self, n):
        """
        Spawns a given number of NPC vehicles.

        Input:
            n: number of NPCs
        """
        spawn_points = self.world.get_map().get_spawn_points()

        for i in range(n):
            vehicle_bp = random.choice(self.bp_lib.filter('vehicle'))
            vehicle_type = vehicle_bp.get_attribute("base_type").as_str()
            if vehicle_type == "bicycle" or vehicle_type == "motorcycle" or vehicle_type == "Bus" or vehicle_type == "":
                i-=1
                continue
            # if vehicle_bp.has_attribute("base_type"):
            #     print("Base type: ", vehicle_bp.get_attribute("base_type").as_str())

            npc = self.world.try_spawn_actor(vehicle_bp, spawn_points[i+2])
            if npc:
                self.vehicle_list.append(npc)
                npc.set_autopilot(True)

        print("Spawned {} NPC vehicles.".format(n))

    def spawn_npc_pedestrians(self, n, hidePatch=False):
        """
        Spawns a given number of NPC pedestrians.

        Input:
            n: number of NPCs
        """
        # -------------
        # Spawn Walkers
        # -------------
        # some settings
        percentagePedestriansRunning = 0.0      # how many pedestrians will run
        percentagePedestriansCrossing = 0.0     # how many pedestrians will walk through the road
        self.world.set_pedestrians_seed(42)
        # 1. take all the random locations to spawn
        spawn_points = []
        for i in range(n):
            spawn_point = carla.Transform()
            loc = self.world.get_random_location_from_navigation()
            if (loc != None):
                spawn_point.location = loc
                spawn_points.append(spawn_point)
        # 2. we spawn the walker object
        batch = []
        walker_speed = []
        # for spawn_point in self.walker_spawn_point:
        for i in range(n):
            spawn_point = spawn_points[i]
            walker_bp = self.bp_lib.filter('walker')[i%len(self.bp_lib.filter('walker'))]
            if walker_bp.has_attribute("age"):
                # print(walker_bp.get_attribute("age").as_str())
                if walker_bp.get_attribute("age").as_str() == "child":
                    walker_bp = self.bp_lib.filter('walker')[i%len(self.bp_lib.filter('walker')) + 2]
            # walker_bp = random.choice(self.bp_lib.filter('walker'))
            # set as not invincible
            if walker_bp.has_attribute('is_invincible'):
                walker_bp.set_attribute('is_invincible', 'false')
            # set the max speed
            if walker_bp.has_attribute('speed'):
                if (random.random() > percentagePedestriansRunning):
                    # walking
                    walker_speed.append(walker_bp.get_attribute('speed').recommended_values[1])
                else:
                    # running
                    walker_speed.append(walker_bp.get_attribute('speed').recommended_values[2])
            else:
                print("Walker has no speed")
                walker_speed.append(0.0)

            batch.append(SpawnActor(walker_bp, spawn_point))

        results = self.client.apply_batch_sync(batch, True)
        walker_speed2 = []
        walkers_list = []
        for i in range(len(results)):
            if results[i].error:
                logging.error(results[i].error)
            else:
                walkers_list.append({"id": results[i].actor_id})
                walker_speed2.append(walker_speed[i])
        walker_speed = walker_speed2
        # 3. we spawn the walker controller
        batch = []
        num_walkers = len(walkers_list)
        walker_controller_bp = self.bp_lib.find('controller.ai.walker')
        for i in range(num_walkers):
            batch.append(SpawnActor(walker_controller_bp, carla.Transform(), walkers_list[i]["id"]))
        results = self.client.apply_batch_sync(batch, True)
        for i in range(len(results)):
            if results[i].error:
                logging.error(results[i].error)
            else:
                walkers_list[i]["con"] = results[i].actor_id
        # 4. we put together the walkers and controllers id to get the objects from their id
        for i in range(len(walkers_list)):
            self.pedestrian_list.append(walkers_list[i]["con"])
            self.pedestrian_list.append(walkers_list[i]["id"])
        all_pedestrians = self.world.get_actors(self.pedestrian_list)

        self.world.tick()

        # 5. initialize each controller and set target to walk to (list is [controler, actor, controller, actor ...])
        # set how many pedestrians can cross the road
        self.world.set_pedestrians_cross_factor(percentagePedestriansCrossing)
        for i in range(0, len(self.pedestrian_list), 2):
            # start walker
            all_pedestrians[i].start()
            # set walk to random point
            all_pedestrians[i].go_to_location(self.world.get_random_location_from_navigation())
            # max speed
            all_pedestrians[i].set_max_speed(float(walker_speed[int(i/2)]))

    def spawn_ego(self, transform):
        """
        Spawns the ego-vehicle and attaches an RGB camera to it.
        """
        vehicle_bp = self.bp_lib.find('vehicle.lincoln.mkz_2020')
        self.car = self.world.try_spawn_actor(vehicle_bp, transform)

        camera_bp = self.bp_lib.find('sensor.camera.rgb')
        camera_init_trans = carla.Transform(carla.Location(x=1.5, z=2.4))
        self.camera = self.world.spawn_actor(camera_bp, camera_init_trans, attach_to=self.car)
        self.camera.listen(self.image_queue.put)


    def get_camera_matrix(self, w, h, fov):
        """
        Calculate projection matrix of the camera (intrinsics).

        Input:
            w: width of image (px)
            h: height of image (px)
            fov: field of view (deg)
        Output:
            K: 3x3 camera matrix
        """
        focal = w / (2.0 * np.tan(fov * np.pi / 360.0))
        K = np.identity(3)
        K[0, 0] = K[1, 1] = focal
        K[0, 2] = w / 2.0
        K[1, 2] = h / 2.0
        return K

    def game_loop(self):
        # delete old images from output folder
        out_folder = os.listdir(OUTPUT_FOLDER)

        for item in out_folder:
            if item.endswith(".png"):
                os.remove(os.path.join(OUTPUT_FOLDER, item))
        try: 
            self.client = carla.Client("localhost", 2000)
            self.client.set_timeout(2.0)
            self.world = self.client.get_world()
            self.bp_lib = self.world.get_blueprint_library()

            self.traffic_manager = self.client.get_trafficmanager()
            self.traffic_manager.set_global_distance_to_leading_vehicle(2.5)
            self.traffic_manager.set_synchronous_mode(True)
            # spectator = self.world.get_spectator()

            labels = [carla.CityObjectLabel.Car,
                      carla.CityObjectLabel.Truck,
                      carla.CityObjectLabel.Motorcycle,
                      carla.CityObjectLabel.Bicycle,
                      carla.CityObjectLabel.Rider,
                      carla.CityObjectLabel.Bus,
                      carla.CityObjectLabel.Train,
                      carla.CityObjectLabel.Pedestrians]

            self.world.reset_all_traffic_lights()

            self.set_synchronous_mode(True)

            self.spawn_npc_vehicles(50)
            # self.spawn_npc_pedestrians(130)

            spawn_points = self.world.get_map().get_spawn_points()

            self.spawn_ego(spawn_points[1])

            self.world.tick()

            # Calculate the camera projection matrix to project from 3D -> 2D and save to camera attributes
            image_w = int(self.camera.attributes["image_size_x"])
            image_h = int(self.camera.attributes["image_size_y"])
            fov = float(self.camera.attributes["fov"])
            self.K = self.get_camera_matrix(image_w, image_h, fov)

            self.car.set_autopilot(True)

            self.world.tick()
            frame_number = 0

            image = self.image_queue.get()
            # Reshape the raw data into an RGB array
            img = np.reshape(np.copy(image.raw_data), (image.height, image.width, 4))

            # Display the image in an OpenCV display window
            cv2.namedWindow('CameraFeed', cv2.WINDOW_AUTOSIZE)
            cv2.imshow('CameraFeed',img)
            cv2.waitKey(1)

            i = 0

            while True:
                self.world.tick()
                image = self.image_queue.get()

                if i%50 == 0:
                    # Retrieve and reshape the image
                    
                    img = np.reshape(np.copy(image.raw_data), (image.height, image.width, 4))   
                    frame_number += 1

                    bb_img = img.copy()

                    # Save frame to annotations json
                    frame_file = "{:05d}.png".format(frame_number)

                    self.ground_truth_annotations["images"].append({
                        "file_name": frame_file,
                        "height": image.height,
                        "width": image.width,
                        "id": frame_number
                    })

                    bounding_boxes, bb_img = GTBoundingBoxes.get_bounding_boxes(self.world, self.car, self.camera, self.K, labels, bb_img)

                    for bb_verts in bounding_boxes:
                        if bb_verts:
                            bb_cocoFormat = [bb_verts[0], bb_verts[1], bb_verts[2]-bb_verts[0], bb_verts[3]-bb_verts[1]]
                            category = self.objectlabel2categoryid[bb_verts[-1]]
                            self.ground_truth_annotations["annotations"].append({
                                "segmentation": [],
                                "area": bb_cocoFormat[2]*bb_cocoFormat[3],
                                "iscrowd": 0,
                                "category_id": category,
                                "image_id": frame_number,
                                "bbox": bb_cocoFormat
                            })

                    cv2.imwrite(os.path.join(OUTPUT_FOLDER, frame_file), img)

                    cv2.imshow('CameraFeed',bb_img)
                    if cv2.waitKey(1) == ord('q'):
                        break

                i += 1

                if frame_number >= 1000:
                    break

        except KeyboardInterrupt:
            pass

        finally:
            self.set_synchronous_mode(False)
            print("Destroying the actors!")

            # Destroy them static attacks
            self.client.apply_batch([carla.command.DestroyActor(x) for x in self.static_attacks])
            
            # Destroy them vehicles
            self.client.apply_batch([carla.command.DestroyActor(x) for x in self.vehicle_list])

            # Stop walker controllers
            all_pedestrians = self.world.get_actors(self.pedestrian_list)
            for i in range(0, len(self.pedestrian_list), 2):
                all_pedestrians[i].stop()
            # Destroy them pedestrians
            print(len(self.pedestrian_list))
            self.client.apply_batch([carla.command.DestroyActor(x) for x in self.pedestrian_list])

            # self.car.destroy()
            # self.camera.destroy()
            if self.car.destroy() and self.camera.destroy():

                print("Destroyed {} vehicles, {} walkers and the ego-vehicle and camera.".format(len(self.vehicle_list), int(len(self.pedestrian_list)/2)))
            
            cv2.destroyAllWindows()

            print("Saving annotations to json file.")
            with open(os.path.join(OUTPUT_FOLDER, 'annotations.json'), 'w') as json_file:
                json.dump(self.ground_truth_annotations, json_file)


if __name__ == "__main__":
    try:
        attack_scenario = StaticAttackScenario()
        attack_scenario.game_loop()
    finally:
        print("EXIT!")