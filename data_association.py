#!/usr/bin/env python
from __future__ import division
import roslib
import rospy
import rosparam
import copy
import numpy as np
import os

from std_msgs.msg import Float32, Header, String                                                                                                                                  
from geometry_msgs.msg import Point, Vector3
from multi_tracker.msg import Contourinfo, Contourlist
from multi_tracker.msg import Trackedobject, Trackedobjectlist

import matplotlib.pyplot as plt
import Kalman
import imp


class DataAssociator(object):
    def __init__(self):
        kalman_parameter_py_file = rospy.get_param('/multi_tracker/data_association/kalman_parameters_py_file')
        home_directory = os.path.expanduser( rospy.get_param('/multi_tracker/home_directory') )
        kalman_parameter_py_file = os.path.join(home_directory, kalman_parameter_py_file)
        
        self.kalman_parameters = imp.load_source('kalman_parameters', kalman_parameter_py_file)
        self.association_matrix = self.kalman_parameters.association_matrix
        self.association_matrix /= np.linalg.norm(self.association_matrix)
        self.max_covariance = self.kalman_parameters.max_covariance
        
        self.tracked_objects = {}
        self.current_objid = 0
        
        #self.min_size = rospy.get_param('/multi_tracker/data_association/min_size')
        #self.max_size = rospy.get_param('/multi_tracker/data_association/max_size')
        self.max_tracked_objects = rospy.get_param('/multi_tracker/data_association/max_tracked_objects')
        self.n_covariances_to_reject_data = rospy.get_param('/multi_tracker/data_association/n_covariances_to_reject_data')
        
        # initialize the node
        rospy.init_node('data_associator')
        
        # Publishers.
        self.pubTrackedObjects = rospy.Publisher('/multi_tracker/tracked_objects', Trackedobjectlist, queue_size=30)
        
        # Subscriptions.
        self.subImage = rospy.Subscriber('/multi_tracker/contours', Contourlist, self.contour_identifier)
        
    def contour_identifier(self, contourlist):
        
        # keep track of which new objects have been "taken"
        contours_accounted_for = []
        
        # pretend there is only one contour
        del contourlist.contours[1:]
        
        update_dict = {}
        
        def update_tracked_object(tracked_object, measurement, contourlist):
            if measurement is None:
                m = np.matrix([np.nan for i in range( tracked_object['measurement'].shape[0] ) ]).T
                xhat, P, K = tracked_object['kalmanfilter'].update( None ) # run kalman filter
            else:
                tracked_object['measurement'] = np.hstack( (tracked_object['measurement'], measurement) ) # add object's data to the tracked object
                xhat, P, K = tracked_object['kalmanfilter'].update( tracked_object['measurement'][:,-1] ) # run kalman filter
            tracked_object['frames'].append(contourlist.header.seq)
            tracked_object['timestamp'].append(contourlist.header.stamp)
            tracked_object['state'] = np.hstack( (tracked_object['state'], xhat) )
        
        # iterate through objects first
        # get order of persistence
        objid_in_order_of_persistance = []
        if len(self.tracked_objects.keys()) > 0:
            persistance = []
            objids = []
            for objid, tracked_object in self.tracked_objects.items():
                 persistance.append(len(tracked_object['frames']))
                 objids.append(objid)
            order = np.argsort(persistance)[::-1]
            objid_in_order_of_persistance = [objids[o] for o in order]

        new_obj = False
        for contour in contourlist.contours:
            contour = contourlist.contours[0]
            measurement = np.matrix([contour.x, contour.y, 0, contour.area, contour.angle]).T
            try:
                tracked_object = self.tracked_objects[0]
            except (IndexError, KeyError):
                new_obj = True
            else:
                update_tracked_object(tracked_object, measurement, contourlist)
                                     
        # any unnaccounted contours should spawn new objects
        
        if new_obj:
            obj_state = np.matrix([contour.x, 0, contour.y, 0, 0, 0, contour.area, 0, contour.angle, 0]).T # pretending 3-d tracking (z and zvel) for now
            obj_measurement = np.matrix([contour.x, contour.y, 0, contour.area, contour.angle]).T
            # If not associated with previous object, spawn a new object
            new_obj = { 'objid':        self.current_objid,
                        'statenames':   {   'position': [0, 2, 4], 
                                            'velocity': [1, 3, 5],
                                            'size': 6,
                                            'd_size': 7,
                                            'angle': 8,
                                            'd_angle': 9,    
                                        },
                        'state':        obj_state,
                        'measurement':  obj_measurement,
                        'timestamp':    [contour.header.stamp],
                        'frames':       [contour.header.seq],
                        'kalmanfilter': Kalman.DiscreteKalmanFilter(x0      = obj_state, 
                                                                    P0      = self.kalman_parameters.P0, 
                                                                    phi     = self.kalman_parameters.phi, 
                                                                    gamma   = self.kalman_parameters.gamma, 
                                                                    H       = self.kalman_parameters.H, 
                                                                    Q       = self.kalman_parameters.Q, 
                                                                    R       = self.kalman_parameters.R, 
                                                                    gammaW  = self.kalman_parameters.gammaW,
                                                                    )
                      }
            self.tracked_objects.setdefault(new_obj['objid'], new_obj)
            self.current_objid += 1
        # propagate unmatched objects
        for objid, tracked_object in self.tracked_objects.items():
            if tracked_object['frames'][-1] != contourlist.header.seq:
                update_tracked_object(tracked_object, None, contourlist)
        
        # make sure we don't get too many objects - delete the oldest ones, and ones with high covariances
        objects_to_destroy = []
        if len(objid_in_order_of_persistance) > self.max_tracked_objects:
            for objid in objid_in_order_of_persistance[self.max_tracked_objects:]:
                objects_to_destroy.append(objid)
        for objid in objects_to_destroy:
            del(self.tracked_objects[objid])
            
        # recalculate persistance (not necessary, but convenient)
        objid_in_order_of_persistance = []
        if len(self.tracked_objects.keys()) > 0:
            persistance = []
            for objid, tracked_object in self.tracked_objects.items():
                 persistance.append(len(tracked_object['frames']))
                 objid_in_order_of_persistance.append(objid)
            order = np.argsort(persistance)[::-1]
            objid_in_order_of_persistance = [objid_in_order_of_persistance[o] for o in order]
        if len(objid_in_order_of_persistance) > 0:
            most_persistant_objid = objid_in_order_of_persistance[0]

        # publish tracked objects
        if 1:
            object_info_to_publish = []
            t = contourlist.header.stamp
            for objid in objid_in_order_of_persistance:
                if objid not in objects_to_destroy:
                    tracked_object = self.tracked_objects[objid]
                    data = Trackedobject()
                    data.header  = Header(stamp=t)
                    p = np.array( tracked_object['state'][tracked_object['statenames']['position'],-1] ).flatten().tolist()
                    v = np.array( tracked_object['state'][tracked_object['statenames']['velocity'],-1] ).flatten().tolist()
                    data.position       = Point( p[0], p[1], p[2] )
                    data.velocity       = Vector3( v[0], v[1], v[2] )
                    data.angle          = tracked_object['state'][tracked_object['statenames']['angle'],-1]
                    data.size           = tracked_object['state'][tracked_object['statenames']['size'],-1]#np.linalg.norm(tracked_object['kalmanfilter'].P.diagonal())
                    data.measurement    = Point( tracked_object['measurement'][0, -1], tracked_object['measurement'][1, -1], 0)
                    tracked_object_covariance = np.linalg.norm( (tracked_object['kalmanfilter'].H*tracked_object['kalmanfilter'].P).T*self.association_matrix )
                    data.covariance     = tracked_object_covariance # position covariance only
                    data.objid          = tracked_object['objid']
                    data.persistence    = len(tracked_object['frames'])
                    object_info_to_publish.append(data)
            header = Header(stamp=t)
            self.pubTrackedObjects.publish( Trackedobjectlist(header=header, tracked_objects=object_info_to_publish) )
        
    def main(self):
        rospy.spin()
                
                
if __name__ == '__main__':
    data_associator = DataAssociator()
    data_associator.main()
