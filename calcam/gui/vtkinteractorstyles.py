'''
* Copyright 2015-2017 European Atomic Energy Community (EURATOM)
*
* Licensed under the EUPL, Version 1.1 or - as soon they
  will be approved by the European Commission - subsequent
  versions of the EUPL (the "Licence");
* You may not use this work except in compliance with the
  Licence.
* You may obtain a copy of the Licence at:
*
* https://joinup.ec.europa.eu/software/page/eupl
*
* Unless required by applicable law or agreed to in
  writing, software distributed under the Licence is
  distributed on an "AS IS" basis,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
  express or implied.
* See the Licence for the specific language governing
  permissions and limitations under the Licence.
'''


"""
vtk Interactor style classes for Calcam.

There's a lot more code in here than there needs to be / should be,
a lot of what's done in here should probably be moved to the GUI module.
However, because a lot of this code predates the Qt GUI, it was never moved out of here,
which is why the GUI window object and vtkinteractor object interact with
each other in a sadly messy way.

Written by Scott Silburn 
"""

import vtk
import numpy as np
from ..render import get_image_actor

class CalcamInteractorStyle3D(vtk.vtkInteractorStyleTerrain):
 
    def __init__(self,parent=None,viewport_callback=None,resize_callback=None,newpick_callback=None,cursor_move_callback=None,focus_changed_callback=None,refresh_callback=None):
        
        # Set callbacks for all the mouse controls
        self.AddObserver("LeftButtonPressEvent",self.on_left_click)
        self.AddObserver("RightButtonPressEvent",self.right_press)
        self.AddObserver("RightButtonReleaseEvent",self.right_release)
        self.AddObserver("MiddleButtonPressEvent",self.middle_press)
        self.AddObserver("MiddleButtonReleaseEvent",self.middle_release)
        self.AddObserver("MouseWheelForwardEvent",self.zoom_in)
        self.AddObserver("MouseWheelBackwardEvent",self.zoom_out)
        self.AddObserver("MouseMoveEvent",self.on_mouse_move)
        self.viewport_callback = viewport_callback
        self.pick_callback = newpick_callback
        self.resize_callback = resize_callback
        self.newpick_callback = newpick_callback
        self.cursor_move_callback = cursor_move_callback
        self.cursor_changed_callback = focus_changed_callback
        self.refresh_callback = refresh_callback
        self.focus_changed_callback = focus_changed_callback


    # Do various initial setup things, most of which can't be done at the time of __init__
    def init(self):

        # Get the interactor object
        self.interactor = self.GetInteractor()

        # Some other objects from higher up which I need access to
        self.vtkwindow = self.interactor.GetRenderWindow()
        renderers = self.vtkwindow.GetRenderers()
        renderers.InitTraversal()
        self.renderer = renderers.GetNextItemAsObject()
        self.camera = self.renderer.GetActiveCamera()

        self.SetAutoAdjustCameraClippingRange(False)

        # Turn off any VTK responses to keyboard input (all necessary keyboard shortcuts etc are done in Q)
        self.interactor.RemoveObservers('KeyPressEvent')
        self.interactor.RemoveObservers('CharEvent')

        # Add observer for catching window resizing
        self.vtkwindow.AddObserver("ModifiedEvent",self.on_resize)


        # Create a picker
        self.picker = vtk.vtkCellPicker()
        self.interactor.SetPicker(self.picker)

        # Variables
        self.cursors = {}
        self.next_cursor_id = 0
        self.focus_cursor = None
        self.legend = None
        self.xsection_coords = None
    

        # We will use this for converting from 3D to screen coords.
        self.vtk_coord_transformer = vtk.vtkCoordinate()
        self.vtk_coord_transformer.SetCoordinateSystemToWorld()



    # Middle click + drag to pan
    def middle_press(self,obj,event):
        self.orig_dist = self.camera.GetDistance()
        self.camera.SetDistance(0.5)
        self.OnMiddleButtonDown()


    def middle_release(self,obj,event):
        self.OnMiddleButtonUp()
        self.camera.SetDistance(self.orig_dist)
        self.on_cam_moved()


    # On the CAD view, right click+drag to rotate (usually on left button in this interactorstyle)
    def right_press(self,obj,event):
        self.orig_dist = self.camera.GetDistance()
        self.camera.SetDistance(0.01)
        self.OnLeftButtonDown()


    def right_release(self,obj,event):
        self.OnLeftButtonUp()
        self.camera.SetDistance(self.orig_dist)
        self.on_cam_moved()



    def zoom_in(self,obj,event):

        # If ctrl + scroll, change the camera FOV
        if self.interactor.GetControlKey():
            self.camera.SetViewAngle(max(self.camera.GetViewAngle()*0.9,1))

        # Otherwise, move the camera forward.
        else:
            orig_dist = self.camera.GetDistance()
            self.camera.SetDistance(0.3)
            self.camera.Dolly(1.5)
            self.camera.SetDistance(orig_dist)

        # Update cursor sizes depending on their distance from the camera,
        # so they're all comfortably visible and clickable.
        self.on_cam_moved()
   


    def zoom_out(self,obj,event):

        # If ctrl + scroll, change the camera FOV
        if self.interactor.GetControlKey():
            self.camera.SetViewAngle(min(self.camera.GetViewAngle()*1.1,110.))

        # Otherwise, move the camera backward.
        else:
            orig_dist = self.camera.GetDistance()
            self.camera.SetDistance(0.3)
            self.camera.Dolly(0.75)
            self.camera.SetDistance(orig_dist)

        # Update cursor sizes so they're all well visible:
        self.on_cam_moved()


    def on_cam_moved(self):
        self.update_cursor_style(refresh=False)
        self.update_clipping()

        if self.viewport_callback is not None:
            self.viewport_callback()


    def set_xsection(self,xsection_coords):

        if xsection_coords is not None:
            self.xsection_coords = xsection_coords
        else:
            self.xsection_coords = None

        self.update_clipping()
    
    def get_xsection(self):
        return self.xsection_coords



    def get_cursor_coords(self,cursor_id):

        return self.cursors[cursor_id]['cursor3d'].GetFocalPoint()


    def update_clipping(self):

        self.renderer.ResetCameraClippingRange()

        if self.xsection_coords is not None:
            normal_range = self.camera.GetClippingRange()
            cam_to_xsec = self.xsection_coords - np.array(self.camera.GetPosition())
            cam_view_dir = self.camera.GetDirectionOfProjection()
            dist = max(normal_range[0],np.dot(cam_to_xsec,cam_view_dir))
            self.camera.SetClippingRange(dist,normal_range[1])

        if self.refresh_callback is not None:
            self.refresh_callback()


    # Left click to move a point or add a new point
    def on_left_click(self,obj,event):

        ctrl_pressed = self.interactor.GetControlKey()

        # These will be the variables we return. If the user clicked in free space they will stay None.
        clicked_cursor = None
        pickcoords = None

        # Do a pick with our picker object
        clickcoords = self.interactor.GetEventPosition()
        retval = self.picker.Pick(clickcoords[0],clickcoords[1],0,self.renderer)

        # If something was successfully picked, find out what it was...
        if retval != 0:

            pickedpoints = self.picker.GetPickedPositions()

            # If more than 1 point is within the picker's tolerance,
            # use the one closest to the camera (this is most intuitive)
            npoints = pickedpoints.GetNumberOfPoints()
            dist_fromcam = []
            campos = self.camera.GetPosition()

            for i in range(npoints):
                point = pickedpoints.GetPoint(i)
                dist_fromcam.append(np.sqrt( (campos[0] - point[0])**2 + (campos[1] - point[1])**2 + (campos[2] - point[2])**2 ))

            _, idx = min((val, idx) for (idx, val) in enumerate(dist_fromcam))

            pickcoords = pickedpoints.GetPoint(idx)

            # If the picked point is within 1.5x the cursor radius of any existing point,
            # say that the user clicked on that point
            dist = 7
            for cid,cursor in self.cursors.items():

                if cid == self.focus_cursor:
                    continue

                # Get the on-screen position of this cursor
                self.vtk_coord_transformer.SetValue(cursor['cursor3d'].GetFocalPoint())
                cursorpos = self.vtk_coord_transformer.GetComputedDisplayValue(self.renderer)
                

                dist_from_cursor = np.sqrt( (cursorpos[0] - clickcoords[0])**2 + (cursorpos[1] - clickcoords[1])**2 )
                if dist_from_cursor < dist:
                        clicked_cursor = cid
                        dist = dist_from_cursor



            # If they held CTRL, we send a new pick callback
            if (ctrl_pressed or self.focus_cursor is None) and clicked_cursor is None:

                if self.newpick_callback is not None:
                    self.newpick_callback(pickcoords)

            else:

                # Otherwise, if they clicked an existing cursor, change the focus to it
                if clicked_cursor is not None:

                    self.set_cursor_focus(clicked_cursor)

                    if self.focus_changed_callback is not None:
                        self.focus_changed_callback(clicked_cursor)

                    if self.cursor_move_callback is not None:
                        self.cursor_move_callback(self.cursors[clicked_cursor]['cursor3d'].GetFocalPoint())

                # of if they didn't click another cursor, move the current cursor
                # to where they clicked
                elif self.focus_cursor is not None:

                    self.cursors[self.focus_cursor]['cursor3d'].SetFocalPoint(pickcoords)
                    self.update_cursor_style()

                    if self.cursor_move_callback is not None:
                        self.cursor_move_callback(pickcoords)


            if self.refresh_callback is not None:
                self.refresh_callback()


    def update_cursor_style(self,refresh=True):

        campos = self.camera.GetPosition()
        for cid,cursor in self.cursors.items():

            position = cursor['cursor3d'].GetFocalPoint()

            dist_to_cam = np.sqrt( (campos[0] - position[0])**2 + (campos[1] - position[1])**2 + (campos[2] - position[2])**2 )

            if cid == self.focus_cursor:
                colour = (0,0.8,0)
                linewidth = 3
                size = 0.025
            else:
                if cursor['colour'] is not None:
                    colour = cursor['colour']
                else:
                    colour = (0.8,0,0)
                linewidth = 2
                size = 0.0125

            # Cursor size scales with camera FOV to maintain size on screen.
            size = size * (self.camera.GetViewAngle()/75)

            cursor['cursor3d'].SetModelBounds([position[0]-size*dist_to_cam,position[0]+size*dist_to_cam,position[1]-size*dist_to_cam,position[1]+size*dist_to_cam,position[2]-size*dist_to_cam,position[2]+size*dist_to_cam])
            cursor['actor'].GetProperty().SetColor(colour)
            cursor['actor'].GetProperty().SetLineWidth(linewidth)

        if self.refresh_callback is not None and refresh:
            self.refresh_callback()



    # Defocus cursors for a given point pair
    def set_cursor_focus(self,cursor_id):

        if cursor_id is not None:
            if cursor_id not in self.cursors.keys():
                raise ValueError('No cursor with ID {:d}'.format(cursor_id))

        self.focus_cursor = cursor_id
        self.update_cursor_style()


    def get_cursor_focus(self):
        return self.focus_cursor


    def add_cursor(self,coords):

        # Create new cursor, mapper and actor
        new_cursor_id = self.next_cursor_id
        self.cursors[new_cursor_id] = {'cursor3d':vtk.vtkCursor3D(),'actor':vtk.vtkActor(),'colour':None}
        self.next_cursor_id += 1

        # Some setup of the cursor
        self.cursors[new_cursor_id]['cursor3d'].XShadowsOff()
        self.cursors[new_cursor_id]['cursor3d'].YShadowsOff()
        self.cursors[new_cursor_id]['cursor3d'].ZShadowsOff()
        self.cursors[new_cursor_id]['cursor3d'].OutlineOff()
        self.cursors[new_cursor_id]['cursor3d'].SetTranslationMode(1)
        self.cursors[new_cursor_id]['cursor3d'].SetFocalPoint(coords[0],coords[1],coords[2])


        # Mapper setup
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(self.cursors[new_cursor_id]['cursor3d'].GetOutputPort())
    
        # Actor setup
        self.cursors[new_cursor_id]['actor'].SetMapper(mapper)

        # Add new cursor to screen
        self.renderer.AddActor(self.cursors[new_cursor_id]['actor'])

        self.update_cursor_style()

        return new_cursor_id
  

    def remove_cursor(self,cursor_id):

        try:
            cursor = self.cursors.pop(cursor_id)
            self.renderer.RemoveActor(cursor['actor'])
            if cursor_id == self.focus_cursor:
                self.focus_cursor = None
                self.update_cursor_style()
        except KeyError:
            raise ValueError('No cursor with ID {:d}'.format(cursor_id))


    def on_resize(self,obj=None,event=None):

        vtk_size = self.vtkwindow.GetSize()

        # Sizing of the legend
        if self.legend is not None:

            legend_offset_y = 0.02
            legend_scale = 0.03

            legend_offset_x = legend_offset_y*vtk_size[1] / vtk_size[0]
            legend_pad_y = 20./vtk_size[1]
            legend_pad_x = 20./vtk_size[0]

            legend_height = legend_pad_y + legend_scale * self.n_legend_items
            abs_height = legend_scale * vtk_size[1]
            width_per_char = abs_height * 0.5
            legend_width = legend_pad_x + (width_per_char * self.longest_name)/vtk_size[0]

            self.legend.GetPosition2Coordinate().SetCoordinateSystemToNormalizedDisplay()
            self.legend.GetPositionCoordinate().SetCoordinateSystemToNormalizedDisplay()

            # Set Legend Size
            self.legend.GetPosition2Coordinate().SetValue(legend_width,legend_height )
            # Set Legend position
            self.legend.GetPositionCoordinate().SetValue(1 - legend_offset_x - legend_width, legend_offset_y)


        if self.resize_callback is not None:
            self.resize_callback(vtk_size)


    def set_legend(self,legend_items):

        if self.legend is not None:
            self.renderer.RemoveActor(self.legend)

        if len(legend_items) > 0:

            self.longest_name = 0
            for entry in legend_items:
                if len(entry[0]) > self.longest_name:
                    self.longest_name = len(entry[0])

            self.n_legend_items = len(legend_items)

            legend = vtk.vtkLegendBoxActor()
            legend.SetNumberOfEntries(len(legend_items))

            for i,entry in enumerate(legend_items):
                legend.SetEntryString(i,entry[0])
                legend.SetEntryColor(i,entry[1])

 
            legend.UseBackgroundOn()
            legend.SetBackgroundColor((0.1,0.1,0.1))
            legend.SetPadding(9)
            self.legend = legend

            self.renderer.AddActor(self.legend)

            self.on_resize()


    def on_mouse_move(self,obj=None,event=None):

        self.OnMouseMove()
        if self.xsection_coords is not None:
            self.update_clipping()





class CalcamInteractorStyle2D(vtk.vtkInteractorStyleTerrain):
 
    def __init__(self,parent=None,newpick_callback=None,cursor_move_callback=None,focus_changed_callback=None,refresh_callback=None):
        # Set callbacks for all the controls
        self.AddObserver("LeftButtonPressEvent",self.on_left_click)
        self.AddObserver("MiddleButtonPressEvent",self.middle_press)
        self.AddObserver("MiddleButtonReleaseEvent",self.middle_release)
        self.AddObserver("MouseWheelForwardEvent",self.zoom_in)
        self.AddObserver("MouseWheelBackwardEvent",self.zoom_out)
        self.AddObserver("MouseMoveEvent",self.mouse_move)
        self.newpick_callback = newpick_callback
        self.cursor_move_callback = cursor_move_callback
        self.focus_changed_callback = focus_changed_callback
        self.refresh_callback = refresh_callback

    # Do various initial setup things, most of which can't be done at the time of __init__
    def init(self):

        # Get the interactor object
        self.interactor = self.GetInteractor()

        # Some other objects from higher up which I need access to
        self.vtkwindow = self.interactor.GetRenderWindow()
        renderers = self.vtkwindow.GetRenderers()
        renderers.InitTraversal()
        self.renderer = renderers.GetNextItemAsObject()

        self.im_dragging = False

        self.camera = self.renderer.GetActiveCamera()
        self.camera.ParallelProjectionOn()


        # Turn off any VTK responses to keyboard input (all necessary keyboard shortcuts etc are done in Qt)
        self.interactor.RemoveObservers('KeyPressEvent')
        self.interactor.RemoveObservers('CharEvent')

        # Add observer for catching window resizing
        self.vtkwindow.AddObserver("ModifiedEvent",self.on_resize)


        # Variables
        self.active_cursors = {}
        self.passive_cursors = {}
        self.next_cursor_id = 0

        self.focus_cursor = None

        self.image_actor = None
        self.overlay_actor = None


    # Use this image object
    def set_image(self,image,n_subviews=1,subview_lookup=lambda x,y: 0,hold_position=False):
        
        # Remove current image, if any
        if self.image_actor is not None:

            self.renderer.RemoveActor(self.image_actor)
            self.image_actor = None
        
        if self.overlay_actor is not None:

            self.renderer.RemoveActor(self.overlay_actor)
            self.overlay_actor = None


        if image is not None:

            self.n_subviews = n_subviews
            self.subview_lookup = subview_lookup

            winsize = self.vtkwindow.GetSize()
            winaspect =  float(winsize[0])/float(winsize[1])

            self.image_actor = get_image_actor(image)

            self.renderer.AddActor2D(self.image_actor)

            bounds = self.image_actor.GetBounds()
            xc = bounds[0] + bounds[1] / 2
            yc = bounds[2] + bounds[3] / 2
            ye = bounds[3] - bounds[2]
            xe = bounds[1] - bounds[0]

            self.zoom_ref_cc = (xc,yc)

            im_aspect = xe / ye

            if winaspect >= im_aspect:
                # Base new zero size on y dimension
                self.zoom_ref_scale = 0.5*ye
            else:
                self.zoom_ref_scale = 0.5*xe/winaspect


            if not hold_position:
                # Reset view to fit the whole image on screen.
                self.zoom_level = 1.                
                self.camera.SetParallelScale(self.zoom_ref_scale)
                self.camera.SetPosition(xc,yc,1.)
                self.camera.SetFocalPoint(xc,yc,0.)
        
        if self.refresh_callback is not None:
            self.refresh_callback()


    # On the CAD view, middle click + drag to pan
    def middle_press(self,obj,event):
        self.im_dragging = True

    def middle_release(self,obj,event):
            self.im_dragging = False


    def set_overlay_image(self,overlay_actor):
        
        if self.overlay_actor is not None:
            self.renderer.RemoveActor(self.overlay_actor)
            self.overlay_actor = None

        self.overlay_actor = overlay_actor
        self.renderer.AddActor2D(self.overlay_actor)

        if self.refresh_callback is not None:
            self.refresh_callback()



    # Left click to move a point or add a new point
    def on_left_click(self,obj,event):

        clicked_cursor = None

        ctrl_pressed = self.interactor.GetControlKey()

        clickcoords = self.interactor.GetEventPosition()

        # Check if the click was near enough an existing cursor to be considered as clicking it
        dist = 7.
        for cid, cursor in self.active_cursors.items():

            if cid == self.focus_cursor:
                continue

            for icursor in cursor['cursor3ds']:
                screencoords = self.image_to_screen_coords(icursor.GetFocalPoint())
                dist_from_cursor = np.sqrt( (screencoords[0] - clickcoords[0])**2 + (screencoords[1] - clickcoords[1])**2 )
                if dist_from_cursor < dist:
                        clicked_cursor = cid
                        dist = dist_from_cursor


        pickcoords = self.screen_to_image_coords(clickcoords)

        bounds = self.image_actor.GetBounds()
        maxind = np.array( [bounds[1] - bounds[0] - 1, bounds[3] - bounds[2] - 1] )

        if np.any(pickcoords < 0) or np.any(pickcoords > maxind):
            return


        if (ctrl_pressed or self.focus_cursor is None) and clicked_cursor is None:

            if self.newpick_callback is not None:
                self.newpick_callback(pickcoords)

        else:

            if clicked_cursor is not None:

                self.set_cursor_focus(clicked_cursor)

                if self.focus_changed_callback is not None:
                    self.focus_changed_callback(clicked_cursor)

                if self.cursor_move_callback is not None:
                    self.cursor_move_callback( self.get_cursor_coords(clicked_cursor) )

            elif self.focus_cursor is not None:

                view_index = self.subview_lookup(pickcoords[0],pickcoords[1])
                if self.active_cursors[self.focus_cursor]['cursor3ds'][view_index] is None:
                    self.add_active_cursor(pickcoords,add_to=self.focus_cursor)
                else:
                    self.set_cursor_coords(self.focus_cursor,pickcoords,view_index)

                    if self.cursor_move_callback is not None:
                        self.cursor_move_callback( self.get_cursor_coords(self.focus_cursor) )


        if self.refresh_callback is not None:
            self.refresh_callback()




    def zoom_in(self,obj,event):


        if self.image_actor is None:
            return

        winsize = self.vtkwindow.GetSize()

        # Re-position the image camera to keep the image point under the mouse pointer fixed when zooming
        zoom_ratio = 1 + 0.2/self.zoom_level                 # The zoom ratio we will have

        # Current camera position and scale
        campos = self.camera.GetPosition()
        camscale = self.camera.GetParallelScale() * 2.

        # Where is the zoom target in world coordinates?

        # Zoom coordinates in window pixels
        zoomcoords = list(self.interactor.GetEventPosition())

        # Position of current centre from to where we're zooming
        zoomcoords = ( (zoomcoords[0] - winsize[0]/2.)/winsize[0] * camscale * float(winsize[0])/float(winsize[1]) + campos[0],
                       (zoomcoords[1] - winsize[1]/2.)/winsize[1] * camscale + campos[1] )

        # Vector from zoom point to current camera centre
        zoomvec = ( campos[0] - zoomcoords[0] , campos[1] - zoomcoords[1] )

        # Now we move the camera along the line bwteen the current camera centre and zoom position
        newxc = zoomvec[0]/zoom_ratio + zoomcoords[0]
        newyc = zoomvec[1]/zoom_ratio + zoomcoords[1]

        # Actually move the camera
        self.camera.SetPosition((newxc,newyc,1.))
        self.camera.SetFocalPoint((newxc,newyc,0.))

        # Actually zoom in.
        self.zoom_level = self.zoom_level + 0.2
        self.camera.SetParallelScale(self.zoom_ref_scale / self.zoom_level)
        self.update_cursor_style()




    def zoom_out(self,obj,event):

        if self.image_actor is None:
            return

        # Zoom out smoothly until the whole image is visible
        if self.zoom_level > 1.:

            zoom_ratio = 0.2/(self.zoom_level**2 - 1.2*self.zoom_level + 0.2)

            campos = self.camera.GetPosition()

            zoomvec = ( self.zoom_ref_cc[0] - campos[0] , self.zoom_ref_cc[1] - campos[1] )

            self.camera.SetPosition((campos[0] + zoomvec[0] * zoom_ratio, campos[1] +  zoomvec[1] * zoom_ratio, 1.))
            self.camera.SetFocalPoint((campos[0] + zoomvec[0] * zoom_ratio, campos[1] +  zoomvec[1] * zoom_ratio, 0.))

            self.zoom_level = self.zoom_level - 0.2
            self.camera.SetParallelScale(self.zoom_ref_scale / self.zoom_level)
            self.update_cursor_style()



    # Defocus cursors for a given point pair
    def set_cursor_focus(self,cursor_id):

        if cursor_id is not None:
            if cursor_id not in self.active_cursors.keys():
                raise ValueError('No cursor with ID {:d}'.format(cursor_id))

        self.focus_cursor = cursor_id
        self.update_cursor_style()


    def get_cursor_focus(self):
        return self.focus_cursor


    # Similar to Set3DCursorStyle but for image points
    def update_cursor_style(self):
        
        camscale = self.camera.GetParallelScale()

        for cid,cursor in self.active_cursors.items():

            for i,icursor in enumerate(cursor['cursor3ds']):
                if icursor is not None:

                    pos = icursor.GetFocalPoint()

                    if self.focus_cursor == cid:
                        colour = (0,0.8,0)
                        linewidth = 3
                        size = 0.03 * camscale
                    else:
                        colour = (0.8,0,0)
                        linewidth = 2
                        size = size = 0.015 * camscale

                    icursor.SetModelBounds(pos[0]-size,pos[0]+size,pos[1]-size,pos[1]+size,0.0,0.0)
                    cursor['actors'][i].GetProperty().SetColor(colour)
                    cursor['actors'][i].GetProperty().SetLineWidth(linewidth)


        size = size = 0.015 * camscale
        for cursor in self.passive_cursors.values():
            pos = cursor['cursor3d'].GetFocalPoint()
            cursor['cursor3d'].SetModelBounds(pos[0]-size,pos[0]+size,pos[1]-size,pos[1]+size,0.,0.)

        if self.refresh_callback is not None:
            self.refresh_callback()


    # Adjust 2D image size and cursor positions if the window is resized
    def on_resize(self,obg=None,event=None):


        if self.image_actor is not None:

            vtksize = self.vtkwindow.GetSize()

            winaspect = float(vtksize[0])/float(vtksize[1])

            bounds = self.image_actor.GetBounds()
            xc = bounds[0] + bounds[1] / 2
            yc = bounds[2] + bounds[3] / 2
            ye = bounds[3] - bounds[2]
            xe = bounds[1] - bounds[0]

            im_aspect = xe / ye

            if winaspect >= im_aspect:
                # Base new zero size on y dimension
                self.zoom_ref_scale = 0.5*ye
            else:
                self.zoom_ref_scale = 0.5*xe/winaspect


            self.camera.SetParallelScale(self.zoom_ref_scale / self.zoom_level)
        


    
    # Function to convert display coordinates to pixel coordinates on the camera image
    def screen_to_image_coords(self,screen_coords):

        vtksize = self.vtkwindow.GetSize()

        camyscale = self.camera.GetParallelScale() * 2.
        camxscale = camyscale * float(vtksize[0])/float(vtksize[1])
        cc = self.camera.GetFocalPoint()
        im_coords = [ (( screen_coords[0] - vtksize[0]/2. ) / vtksize[0]) * camxscale + cc[0], (screen_coords[1] - vtksize[1]/2.)/vtksize[1] * camyscale + cc[1]]

        bounds = self.image_actor.GetBounds()
        ysize = bounds[3] - bounds[2]

        im_coords[-1] = ysize - im_coords[-1]

        return np.array(im_coords)



    def image_to_screen_coords(self,image_coords):

        vtksize = self.vtkwindow.GetSize()

        camyscale = self.camera.GetParallelScale() * 2.
        camxscale = camyscale * float(vtksize[0])/float(vtksize[1])
        cc = self.camera.GetFocalPoint()

        screen_coords = ( float(image_coords[0] - cc[0]) / camxscale * vtksize[0] + vtksize[0]/2. , float(image_coords[1] - cc[1]) / camyscale*vtksize[1] + vtksize[1]/2.)

        return np.array(screen_coords)




    # Add a new point on the image
    def add_active_cursor(self,coords,add_to=None):

        subview = self.subview_lookup(coords[0],coords[1])

        if add_to is None:
            
            new_cursor_id = self.next_cursor_id
            self.next_cursor_id += 1

            self.active_cursors[new_cursor_id] = {'cursor3ds':[None]*self.n_subviews,'actors':[None]*self.n_subviews}
        else:
            new_cursor_id = add_to

        bounds = self.image_actor.GetBounds()
        ysize = bounds[3] - bounds[2]

        # Create new cursor and set it up
        new_cursor = vtk.vtkCursor3D()

        # Some setup of the cursor
        new_cursor.OutlineOff()
        new_cursor.XShadowsOff()
        new_cursor.YShadowsOff()
        new_cursor.ZShadowsOff()
        new_cursor.AxesOn()
        new_cursor.TranslationModeOn()

        new_cursor.SetFocalPoint([coords[0],ysize-coords[1],0.05])

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(new_cursor.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)

        self.active_cursors[new_cursor_id]['actors'][subview] = actor
        self.active_cursors[new_cursor_id]['cursor3ds'][subview] = new_cursor


        # Add new cursor to screen
        self.renderer.AddActor(actor)

        if self.cursor_move_callback is not None:
            self.cursor_move_callback( [cursor.GetFocalPoint() for cursor in self.active_cursors[new_cursor_id]['cursor3ds']] )

        self.update_cursor_style()

        if self.refresh_callback is not None:
            self.refresh_callback()

        return new_cursor_id



    def remove_active_cursor(self,cursor_id):

        if cursor_id not in self.active_cursors:
            raise ValueError('No such cursor ID {:d} exists!'.format(cursor_id))

        cursor = self.active_cursors.pop(cursor_id)

        for actor in cursor['actors']:
            self.renderer.RemoveActor(actor)

        if self.focus_cursor == cursor_id:
            self.focus_cursor = None

            if self.focus_changed_callback is not None:
                self.focus_changed_callback(None)

        if self.refresh_callback is not None:
            self.refresh_callback()


    def get_cursor_coords(self,cursor_id):

        if cursor_id not in self.active_cursors:
            raise ValueError('No such cursor ID {:d} exists!'.format(cursor_id))        

        coords = []

        bounds = self.image_actor.GetBounds()
        ysize = bounds[3] - bounds[2]

        for cursor in self.active_cursors[cursor_id]['cursor3ds']:
            icoords = np.array(cursor.GetFocalPoint()[:2])
            icoords[-1] = ysize - icoords[-1]
            coords.append(icoords)

        return coords


    def set_cursor_coords(self,cursor_id,coords,subview=None):

        if cursor_id not in self.active_cursors:
            raise ValueError('No such cursor ID {:d} exists!'.format(cursor_id))   

        if subview is None and len(coords) != len(self.active_cursors[cursor_id]['cursor3ds']):
            raise ValueError('Expected {:d} sets of coordinates; instead got {:d}.'.format(len(self.active_cursors[cursor_id]['cursor3ds']),len(coords)))

        bounds = self.image_actor.GetBounds()
        ysize = bounds[3] - bounds[2]

        if subview is None:
            for i,cursor in enumerate(self.active_cursors[cursor_id]['cursor3ds']):
                cursor.SetFocalPoint(coords[i][0],ysize-coords[i][1],0.05)
        else:
            self.active_cursors[cursor_id]['cursor3ds'][subview].SetFocalPoint(coords[0],ysize-coords[1],0.05)


    # Show the current CAD points re-projected on to the image
    # using the current fit.
    def add_passive_cursor(self,coords):
            
        new_cursor_id = self.next_cursor_id
        self.next_cursor_id += 1

        bounds = self.image_actor.GetBounds()
        ysize = bounds[3] - bounds[2]

        # Create new cursor and set it up
        new_cursor = vtk.vtkCursor3D()

        # Some setup of the cursor
        new_cursor.OutlineOff()
        new_cursor.XShadowsOff()
        new_cursor.YShadowsOff()
        new_cursor.ZShadowsOff()
        new_cursor.AxesOn()
        new_cursor.TranslationModeOn()

        new_cursor.SetFocalPoint([coords[0],ysize-coords[1],0.05])

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(new_cursor.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor((0,0,1.))
        actor.GetProperty().SetLineWidth(2)

        self.passive_cursors[new_cursor_id] = {'cursor3d':new_cursor,'actor': actor}

        # Add new cursor to screen
        self.renderer.AddActor(actor)

        self.update_cursor_style()

        if self.refresh_callback is not None:
            self.refresh_callback()

        return new_cursor_id


    def remove_passive_cursor(self,cursor_id):

        if cursor_id not in self.passive_cursors:
            raise ValueError('No such cursor ID {:d} exists!'.format(cursor_id))

        cursor = self.passive_cursors.pop(cursor_id)

        self.renderer.RemoveActor(cursor['actor'])

        if self.refresh_callback is not None:
            self.refresh_callback()


    def clear_passive_cursors(self):

        for cursor in self.passive_cursors.values():
            self.renderer.RemoveActor(cursor['actor'])

        self.passive_cursors = {}

        if self.refresh_callback is not None:
            self.refresh_callback()



    # Custom mouse move event to enable middle click panning on both
    # CAD and image views.
    def mouse_move(self,obj,event):

        if self.im_dragging and self.zoom_level > 1:

            winsize = self.vtkwindow.GetSize()

            lastXYpos = self.interactor.GetLastEventPosition() 
            xypos = self.interactor.GetEventPosition()
            camscale = self.camera.GetParallelScale() * 2
            oldpos = self.camera.GetPosition()
            deltaX = (xypos[0] - lastXYpos[0])/float(winsize[0]) * camscale * float(winsize[0])/float(winsize[1])
            deltaY = (xypos[1] - lastXYpos[1])/float(winsize[1]) * camscale

            newY = oldpos[1] - deltaY
            newX = oldpos[0] - deltaX


            # Make sure we don't pan outside the image.
            im_bounds = self.image_actor.GetBounds()
            xcamscale = camscale * float(winsize[0])/winsize[1]
            if newX + xcamscale/2. > im_bounds[1] and newX - xcamscale/2. > im_bounds[0]:
                newX = oldpos[0]
            elif newX - xcamscale/2. < im_bounds[0] and newX + xcamscale/2. < im_bounds[1]:
                newX = oldpos[0]
            if newY + camscale/2. > im_bounds[3] and newY - camscale/2. > im_bounds[2]:
                newY = oldpos[1]
            elif newY - camscale/2. < im_bounds[2] and newY + camscale/2. < im_bounds[3]:
                newY = oldpos[1]

            # Move image camera
            self.camera.SetPosition((newX, newY,1.))
            self.camera.SetFocalPoint((newX,newY,0.))

            if self.refresh_callback is not None:
                self.refresh_callback()