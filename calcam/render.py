'''
* Copyright 2015-2021 European Atomic Energy Community (EURATOM)
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

import vtk
import cv2
from vtk.util.numpy_support import vtk_to_numpy
import numpy as np
import time
from .raycast import raycast_sightlines, RayData
import copy
from .misc import bin_image, get_contour_intersection, LoopProgPrinter, ColourCycle
from .calibration import Calibration
from matplotlib.cm import get_cmap

# This is the maximum image dimension which we expect VTK can succeed at rendering in a single RenderWindow.
# Hopefully 5120 is conservative enough to be safe on most systems but also not restrict render quality too much.
# If getting blank images when trying to render at high resolutions, try reducing this.
# TODO: Add a function to this module to determine the best value for this automatically
max_render_dimension = 5120

def render_cam_view(cadmodel,calibration,extra_actors=[],filename=None,oversampling=1,aa=1,transparency=False,verbose=True,coords = 'display',interpolation='cubic'):
    '''
    Render an image of a given CAD model from the point of view of a given calibration.

    NOTE: This function uses off-screen OpenGL rendering which fails above some image dimension which depends on the system.
    The workaround for this is that above a render dimension set by calcam.render.max_render_dimension, the image is rendered
    at lower resolution and then scaled up using nearest-neighbour scaling. For this reason, when rendering high resolution
    images the rendered image quality may be lower than expected.

    Parameters:

        cadmodel (calcam.CADModel)          : CAD model of scene
        calibration (calcam.Calibration)    : Calibration whose point-of-view to render from.
        extra_actors (list of vtk.vtkActor) : List containing any additional vtkActors to add to the scene \
                                              in addition to the CAD model.
        filename (str)                      : Filename to which to save the resulting image. If not given, no file is saved.
        oversampling (float)                : Used to render the image at higher (if > 1) or lower (if < 1) resolution than the \
                                              calibrated camera. Must be an integer if > 1 or if <1, 1/oversampling must be a \
                                              factor of both image width and height.
        aa (int)                            : Anti-aliasing factor, 1 = no anti-aliasing.
        transparency (bool)                 : If true, empty areas of the image are set transparent. Otherwise they are black.
        verbose (bool)                      : Whether to print status updates while rendering.
        coords (str)                        : Either ``Display`` or ``Original``, the image orientation in which to return the image.
        interpolation(str)                  : Either ``nearest`` or ``cubic``, inerpolation used when applying lens distortion.

    Returns:
        
        np.ndarray                          : Array containing the rendered 8-bit per channel RGB (h x w x 3) or RGBA (h x w x 4) image.\
                                              Also saves the result to disk if the filename parameter is set.
    '''
    if np.any(calibration.view_models) is None:
        raise ValueError('This calibration object does not contain any fit results! Cannot render an image without a calibration fit.')

    if interpolation.lower() == 'nearest':
        interp_method = cv2.INTER_NEAREST
    elif interpolation.lower() == 'cubic':
        interp_method = cv2.INTER_CUBIC
    else:
        raise ValueError('Invalid interpolation method "{:s}": must be "nearest" or "cubic".'.format(interpolation))

    aa = int(max(aa,1))

    if oversampling > 1:
        if int(oversampling) - oversampling > 1e-5:
            raise ValueError('If using oversampling > 1, oversampling must be an integer!')

    elif oversampling < 1:
        shape = calibration.geometry.get_display_shape()
        undersample_x = oversampling * shape[0]
        undersample_y = oversampling * shape[1]

        if abs(int(undersample_x) - undersample_x) > 1e-5 or abs(int(undersample_y) - undersample_y) > 1e-5:
            raise ValueError('If using oversampling < 1, 1/oversampling must be a common factor of the display image width and height ({:d}x{:d})'.format(shape[0],shape[1]))

    if verbose:
        tstart = time.time()
        print('[Calcam Renderer] Preparing...')

    # This will be our result. To start with we always render in display coords.
    orig_display_shape = calibration.geometry.get_display_shape()
    output = np.zeros([int(orig_display_shape[1]*oversampling),int(orig_display_shape[0]*oversampling),3+transparency],dtype='uint8')

    # The un-distorted FOV is over-rendered to allow for distortion.
    # FOV_factor is how much to do this by; too small and image edges might be cut off.
    models = []
    for view_model in calibration.view_models:
        try:
            models.append(view_model.model)
        except AttributeError:
            pass
    if np.any( np.array(models) == 'fisheye'):
        fov_factor = 3.
    else:
        fov_factor = 1.5

    x_pixels = orig_display_shape[0]
    y_pixels = orig_display_shape[1]

    renwin = vtk.vtkRenderWindow()
    renwin.OffScreenRenderingOn()
    renwin.SetBorders(0)

    # Set up render window for initial, un-distorted window
    renderer = vtk.vtkRenderer()
    renwin.AddRenderer(renderer)
    camera = renderer.GetActiveCamera()

    cad_linewidths = np.array(cadmodel.get_linewidth())
    cadmodel.set_linewidth(list(cad_linewidths*aa))
    cadmodel.add_to_renderer(renderer)

    for actor in extra_actors:
        actor.GetProperty().SetLineWidth( actor.GetProperty().GetLineWidth() * aa)
        renderer.AddActor(actor)

    # We need a field mask the same size as the output
    fieldmask = cv2.resize(calibration.get_subview_mask(coords='Display'),(int(x_pixels*oversampling),int(y_pixels*oversampling)),interpolation=cv2.INTER_NEAREST)

    for field in range(calibration.n_subviews):

        render_shrink_factor = 1

        if calibration.view_models[field] is None:
            continue

        cx = calibration.view_models[field].cam_matrix[0,2]
        cy = calibration.view_models[field].cam_matrix[1,2]
        fy = calibration.view_models[field].cam_matrix[1,1]

        vtk_win_im = vtk.vtkWindowToImageFilter()
        vtk_win_im.SetInput(renwin)

        # Width and height - initial render will be put optical centre in the window centre
        width = int(2 * fov_factor * max(cx, x_pixels - cx))
        height = int(2 * fov_factor * max(cy, y_pixels - cy))

        # To avoid trying to render an image larger than the available OpenGL texture buffer,
        # check if the image will be too big and if it will, first try reducing the AA, and if
        # that isn't enough we will render a smaller image and then resize it afterwards.
        # Not ideal but avoids ending up with black images. What would be nicer, is if
        # VTK could fix their large image rendering code to preserve the damn field of view properly!
        longest_side = max(width,height)*aa*oversampling

        while longest_side > max_render_dimension and aa > 1:
            aa = aa - 1
            longest_side = max(width, height) * aa * oversampling

        while longest_side > max_render_dimension:
            render_shrink_factor = render_shrink_factor + 1
            longest_side = max(width,height)*aa*oversampling/render_shrink_factor

        renwin.SetSize(int(width*aa*oversampling/render_shrink_factor),int(height*aa*oversampling/render_shrink_factor))

        # Set up CAD camera
        fov_y = 360 * np.arctan( height / (2*fy) ) / 3.14159
        cam_pos = calibration.get_pupilpos(subview=field)
        cam_tar = calibration.get_los_direction(cx,cy,subview=field) + cam_pos
        upvec = -1.*calibration.get_cam_to_lab_rotation(subview=field)[:,1]
        camera.SetPosition(cam_pos)
        camera.SetViewAngle(fov_y)
        camera.SetFocalPoint(cam_tar)
        camera.SetViewUp(upvec)

        if verbose:
            print('[Calcam Renderer] Rendering (Sub-view {:d}/{:d})...'.format(field + 1,calibration.n_subviews))

        # Do the render and grab an image
        renwin.Render()

        # Make sure the light lights up the whole model without annoying shadows or falloff.
        light = renderer.GetLights().GetItemAsObject(0)
        light.PositionalOn()
        light.SetConeAngle(180)

        # Do the render and grab an image
        renwin.Render()

        vtk_win_im.Update()

        vtk_image = vtk_win_im.GetOutput()
        vtk_array = vtk_image.GetPointData().GetScalars()
        dims = vtk_image.GetDimensions()

        im = np.flipud(vtk_to_numpy(vtk_array).reshape(dims[1], dims[0] , 3))

        # If we have had to do a smaller render for graphics driver reasons, scale the render up to the resolution
        # we really wanted.
        if render_shrink_factor > 1:
            im = cv2.resize(im,(width*aa*oversampling,height*aa*oversampling),interpolation=cv2.INTER_NEAREST)

        if transparency:
            alpha = 255 * np.ones([np.shape(im)[0],np.shape(im)[1]],dtype='uint8')
            alpha[np.sum(im,axis=2) == 0] = 0
            im = np.dstack((im,alpha))

        if verbose:
            print('[Calcam Renderer] Applying lens distortion (Sub-view {:d}/{:d})...'.format(field + 1,calibration.n_subviews))

        # Pixel locations we want on the final image
        [xn,yn] = np.meshgrid(np.linspace(0,x_pixels-1,int(x_pixels*oversampling*aa)),np.linspace(0,y_pixels-1,int(y_pixels*oversampling*aa)))

        xn,yn = calibration.normalise(xn,yn,field)

        # Transform back to pixel coords where we want to sample the un-distorted render.
        # Both x and y are divided by Fy because the initial render always has Fx = Fy.
        xmap = (xn * fy * oversampling * aa) + (width * oversampling * aa - 1)/2
        ymap = (yn * fy * oversampling * aa) + (height * oversampling * aa - 1)/2
        xmap = xmap.astype('float32')
        ymap = ymap.astype('float32')

        # Actually apply distortion
        im  = cv2.remap(im,xmap,ymap,interp_method)

        # Anti-aliasing by binning
        if aa > 1:
            im = bin_image(im,aa,np.mean)

        output[fieldmask == field,:] = im[fieldmask == field,:]


    if coords.lower() == 'original':
        output = calibration.geometry.display_to_original_image(output,interpolation=interpolation)
    
    if verbose:
        print('[Calcam Renderer] Completed in {:.1f} s.'.format(time.time() - tstart))

    # Save the image if given a filename
    if filename is not None:

        # If we have transparency, we can only save as PNG.
        if transparency and filename[-3:].lower() != 'png':
            print('[Calcam Renderer] Images with transparency can only be saved as PNG! Overriding output file type to PNG.')
            filename = filename[:-3] + 'png'

        # Re-shuffle the colour channels for saving (openCV needs BGR / BGRA)
        save_im = copy.copy(output)
        save_im[:,:,:3] = save_im[:,:,2::-1]
        cv2.imwrite(filename,save_im)
        if verbose:
            print('[Calcam Renderer] Result saved as {:s}'.format(filename))


    # Tidy up after ourselves!
    cadmodel.set_linewidth(list(cad_linewidths))
    cadmodel.remove_from_renderer(renderer)

    for actor in extra_actors:
        actor.GetProperty().SetLineWidth( actor.GetProperty().GetLineWidth() / aa ) 
        renderer.RemoveActor(actor)

    renwin.Finalize()

    return output



def render_hires(renderer,oversampling=1,aa=1,transparency=False):
    """
    Render the contents of an existing vtkRenderer to an image array, if requested at higher resolution
    than the existing renderer's current size. N.B. if calling with oversampling > 1 or aa > 1 and using
    VTK versions > 8.2, the returned images will have a slightly smaller field of view than requested. This
    is a known problem caused by the behaviour of VTK; I don't currently have a way to do anything about it.

    Parameters:

        renderer (vtk.vtkRenderer)  : Renderer from which to create the image
        oversampling (int)          : Factor by which to enlarge the resolution
        aa (int)                    : Factor by which to anti-alias by rendering at higher resolution then re-sizing down
        transparency (bool)         : Whether to make black image areas transparent

    """

    # Thicken up all the lines according to AA setting to make sure
    # they don't end upp invisibly thin.
    actorcollection = renderer.GetActors()
    actorcollection.InitTraversal()
    actor = actorcollection.GetNextItemAsObject()
    while actor is not None:
        actor.GetProperty().SetLineWidth( actor.GetProperty().GetLineWidth() * aa )
        actor = actorcollection.GetNextItemAsObject()

    hires_renderer = vtk.vtkRenderLargeImage()
    hires_renderer.SetInput(renderer)
    hires_renderer.SetMagnification( oversampling * aa )

    renderer.Render()
    hires_renderer.Update()
    vtk_im = hires_renderer.GetOutput()
    dims = vtk_im.GetDimensions()

    vtk_im_array = vtk_im.GetPointData().GetScalars()
    im = np.flipud(vtk_to_numpy(vtk_im_array).reshape(dims[1], dims[0] , 3))

    # Put all the line widths back to normal
    actorcollection.InitTraversal()
    actor = actorcollection.GetNextItemAsObject()
    while actor is not None:
        actor.GetProperty().SetLineWidth( actor.GetProperty().GetLineWidth() / aa )
        actor = actorcollection.GetNextItemAsObject()


    if transparency:
        alpha = 255 * np.ones([np.shape(im)[0],np.shape(im)[1]],dtype='uint8')
        alpha[np.sum(im,axis=2) == 0] = 0
        im = np.dstack((im,alpha))

    # Anti-aliasing by binning
    im = bin_image(im,aa,np.mean)

    return im


def get_fov_actor(cadmodel,calib,actor_type='volume',resolution=None):


    if actor_type.lower() == 'volume':

        if resolution is None:
            resolution = 64

    elif actor_type.lower() == 'lines':

        if resolution is None:
            resolution = 32

    else:
        raise ValueError('"actor_type" argument must be "volume" or "lines"')

    raydata = raycast_sightlines(calib,cadmodel,binning=max(calib.geometry.get_display_shape())/resolution,verbose=False)

    # Before we do anything, we need to arrange our triangle corners
    points = vtk.vtkPoints()

    if actor_type.lower() == 'volume': 

        x_horiz,y_horiz = np.meshgrid( np.arange(raydata.ray_start_coords.shape[1]-1), np.arange(raydata.ray_start_coords.shape[0]))
        x_horiz = x_horiz.flatten()
        y_horiz = y_horiz.flatten()

        x_vert,y_vert = np.meshgrid( np.arange(raydata.ray_start_coords.shape[1]), np.arange(raydata.ray_start_coords.shape[0]-1))
        x_vert = x_vert.flatten()
        y_vert = y_vert.flatten()
        
        polygons = vtk.vtkCellArray()

        for n in range(len(x_horiz)):
            if np.abs(raydata.ray_start_coords[y_horiz[n],x_horiz[n],:] - raydata.ray_start_coords[y_horiz[n],x_horiz[n]+1,:]).max() < 1e-3:
                points.InsertNextPoint(raydata.ray_start_coords[y_horiz[n],x_horiz[n],:])
                points.InsertNextPoint(raydata.ray_end_coords[y_horiz[n],x_horiz[n],:])
                points.InsertNextPoint(raydata.ray_end_coords[y_horiz[n],x_horiz[n]+1,:])

        for n in range(len(x_vert)):
            if np.abs( raydata.ray_start_coords[y_vert[n],x_vert[n],:] - raydata.ray_start_coords[y_vert[n]+1,x_vert[n],:] ).max() < 1e-3:
                points.InsertNextPoint(raydata.ray_start_coords[y_vert[n],x_vert[n],:])
                points.InsertNextPoint(raydata.ray_end_coords[y_vert[n],x_vert[n],:])
                points.InsertNextPoint(raydata.ray_end_coords[y_vert[n]+1,x_vert[n],:])


        # Go through and make polygons!
        polygons = vtk.vtkCellArray()
        for n in range(0,points.GetNumberOfPoints(),3):
            polygon = vtk.vtkPolygon()
            polygon.GetPointIds().SetNumberOfIds(3)
            for i in range(3):
                polygon.GetPointIds().SetId(i,i+n)
            polygons.InsertNextCell(polygon)


        # Make Polydata!
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetPolys(polygons)


    elif actor_type.lower() == 'lines':

        lines = vtk.vtkCellArray()
        point_ind = -1
        for i in range(raydata.ray_end_coords.shape[0]):
            for j in range(raydata.ray_end_coords.shape[1]):
                points.InsertNextPoint(raydata.ray_end_coords[i,j,:])
                points.InsertNextPoint(raydata.ray_start_coords[i,j,:])
                point_ind = point_ind + 2

                line = vtk.vtkLine()
                line.GetPointIds().SetId(0,point_ind - 1)
                line.GetPointIds().SetId(1,point_ind)
                lines.InsertNextCell(line)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(lines)


    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().LightingOff()

    if actor_type == 'lines':
        actor.GetProperty().SetLineWidth(2)

    return actor


def get_wall_coverage_actor(cal,cadmodel=None,image=None,imagecoords='Original',clim=None,lower_transparent=True,cmap='jet',clearance=2e-3):
    """
    Get a VTK actor representing the wall area coverage of a given camera.
    Optionally, pass an image to colour the actor according to the image (e.g. to map data to the wall for visualisation)

    Parameters:
        cal                        : Calcam calibration to use - can be an instance of calcam.Calibration or calcam.RayData
        cadmodel (calcam.CADModel) : Calcam CAD model to use for wall. Must be given if 'cal' is calcam.Calibration
        image (np.ndarray)         : Data image to use for colouring. Can be EITHER a h*w size array of any type \
                                     which will be colour mapped, or an 8-bit RGB image for colour data.
        imagecoords (str)          : If passing an image, whether that image is in display or original orientation
        clim (2 element sequence)  : If passing data toe colour mapped, the colour limits. Default is full range of data.
        lower_transparent (bool)   : For colour mapped images, whether to make pixels below the lower colour limit \
                                     transparent (don't show them) or give them the lowest colour in the colour map.
        cmap                       : For colour mapped data, the name of the matplotlib colour map to use.
        clearance (float)          : A distance in metres, the returned actor will be slightly in front of the wall surface by \
                                     this much, in the direction towards the camera. Used to ensure this actor does not get buried \
                                     inside the CAD geometry.

    Returns:

        vtkActor for a wall-hugging surface showing the camera coverage and/or image.

    """

    # If an image is supplied, check & organise it
    if image is not None:

        fr = np.array(image).copy().astype(np.float32)


        if len(image.shape) < 3:
            # Single channel image: will be colour mapped so normalise to CLIM and load colour map
            if clim is None:
                clim = [np.nanmin(image), np.nanmax(image)]

            if lower_transparent:
                fr[fr < clim[0]] = np.nan
            else:
                fr[fr < clim[0]] = clim[0]

            fr[fr > clim[1]] = clim[1]

            fr = (fr - clim[0]) / (clim[1] - clim[0])

            cmap = get_cmap(cmap)

        else:
            # > single channel, assume 8-bit RGB: ensure correct datatype and throw away any extra channels
            fr = fr[:,:,:3].astype(np.uint8)


    if isinstance(cal,Calibration):
        # If we're given a calcam calibration, ray cast to get the wall coords
        if cadmodel is None:
            raise Exception('If passing a Calcam calibration object, a CAD model must also be given!')
        if imagecoords.lower() == 'display':
            w,h = cal.geometry.get_display_shape()
        elif imagecoords.lower() == 'original':
            w, h = cal.geometry.get_original_shape()
        x = np.linspace(- 0.5, w - 0.5, w  + 1)
        y = np.linspace(- 0.5, h - 0.5, h + 1)
        x,y = np.meshgrid(x,y)
        rd = raycast_sightlines(cal,cadmodel,x=x,y=y,coords=imagecoords)
        
    elif isinstance(cal,RayData):
        # If already given a raydata object, just use that!
        rd = cal

    ray_start = rd.get_ray_start()
    ray_dir = rd.get_ray_directions()
    ray_len = rd.get_ray_lengths()

    # Ray end coordinates at the wall: put them 2mm in front of the wall to make sure the resulting actor
    # is not part buried in the CAD model
    ray_end = ray_start + (np.tile(ray_len[:,:,np.newaxis],(1,1,3)) - clearance) * ray_dir



    # VTK objects with coordinates of each pixel corner.
    # Which index in the VTK array corresponds to which pixel is tracked in pointinds
    verts = vtk.vtkPoints()
    pointinds = np.zeros((ray_end.shape[0],ray_end.shape[1]),dtype=int)
    ci = 0
    for i in range(ray_end.shape[0]):
        for j in range(ray_end.shape[1]):
            verts.InsertNextPoint(*ray_end[i,j,:])
            pointinds[i,j] = ci
            ci += 1

    # Create VTK polygon array
    polys  = vtk.vtkCellArray()

    # If we're mapping an image, create the array of colours for each polygon
    if image is not None:
        colours = vtk.vtkUnsignedCharArray()
        colours.SetNumberOfComponents(3)


    # Go through the image
    for xi in range(ray_end.shape[1]-1):
        for yi in range(ray_end.shape[0] - 1):

            # Don't map any pixels which are NaN
            if image is not None:
                if np.isnan(fr[yi,xi]):
                    continue

            # Coordinates and indices of corners for this pixel
            polycoords = ray_end[yi:yi+2,xi:xi+2,:]
            inds = pointinds[yi:yi+2,xi:xi+2]

            # Check if a pixel is "torn" in real space by the ratio of its
            # longest and shortest sides. If it's >4, don't show the pixel.
            deltas = np.zeros((6,3))
            deltas[0,:] = polycoords[0,1,:] - polycoords[0,0,:]
            deltas[1,:] = polycoords[1,1,:] - polycoords[0,1,:]
            deltas[2,:] = polycoords[1,0,:] - polycoords[1,1,:]
            deltas[3,:] = polycoords[0,0,:] - polycoords[1,0,:]
            deltas[4,:] = polycoords[0,0,:] - polycoords[1,1,:]
            deltas[5, :] = polycoords[0,1,:] - polycoords[1,0,:]

            dr = np.sqrt(np.sum(deltas**2,axis=1))

            if dr.max() > 5*dr.min():
                continue

            # Create a quad representing the pixel
            poly = vtk.vtkPolygon()
            poly.GetPointIds().SetNumberOfIds(4)
            poly.GetPointIds().SetId(0,inds[0,0])
            poly.GetPointIds().SetId(1, inds[1, 0])
            poly.GetPointIds().SetId(2, inds[1, 1])
            poly.GetPointIds().SetId(3, inds[0, 1])
            polys.InsertNextCell(poly)

            # If we're mapping an image, colour the quad according to the image data
            if image is not None:
                if len(image.shape) < 3:
                    rgb = (np.array(cmap(fr[yi,xi]))[:-1] * 255).astype(np.uint8)
                    colours.InsertNextTypedTuple(rgb)
                else:
                    colours.InsertNextTypedTuple(fr[yi,xi,:])

    # Put it all togetehr in a vtkPolyDataActor
    polydata = vtk.vtkPolyData()
    polydata.SetPoints(verts)
    polydata.SetPolys(polys)

    if image is not None:
        polydata.GetCellData().SetScalars(colours)

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)

    if image is not None:
        mapper.SetColorModeToDirectScalars()

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)

    return actor

def get_markers_actor(x,y,z,size=1e-2,colour=(1,1,1),flat_shading=True,resolution=12):

    markers = vtk.vtkAssembly()
    for x_,y_,z_ in zip(x,y,z):
        sphere = vtk.vtkSphereSource()
        sphere.SetCenter(x_,y_,z_)
        sphere.SetRadius(size/2)
        sphere.SetPhiResolution(resolution)
        sphere.SetThetaResolution(resolution)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(sphere.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(colour)
        if flat_shading:
            actor.GetProperty().LightingOff()
        markers.AddPart(actor)

    return markers


def get_wall_contour_actor(wall_contour,actor_type='contour',phi=None,toroidal_res=128):

    if actor_type == 'contour' and phi is None:
        raise ValueError('Toroidal angle must be specified if type==contour!')

    points = vtk.vtkPoints()

    if actor_type == 'contour':

        lines = vtk.vtkCellArray()
        x = wall_contour[-1,0]*np.cos(phi)
        y = wall_contour[-1,0]*np.sin(phi)
        points.InsertNextPoint(x,y,wall_contour[-1,1])

        for i in range(wall_contour.shape[0]-1):
            x = wall_contour[i,0]*np.cos(phi)
            y = wall_contour[i,0]*np.sin(phi)
            points.InsertNextPoint(x,y,wall_contour[i,1])

            line = vtk.vtkLine()
            line.GetPointIds().SetId(0,i)
            line.GetPointIds().SetId(1,i+1)
            lines.InsertNextCell(line)

        line = vtk.vtkLine()
        line.GetPointIds().SetId(0,i+1)
        line.GetPointIds().SetId(1,0)
        lines.InsertNextCell(line)

        polydata = polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(lines)

    elif actor_type == 'surface':

        polygons = vtk.vtkCellArray()

        npoints = 0
        for i in range(wall_contour.shape[0]):
            points.InsertNextPoint(wall_contour[i,0],0,wall_contour[i,1])
            npoints = npoints + 1

        tor_step = 3.14159*(360./toroidal_res)/180
        for phi in np.linspace(tor_step,2*3.14159-tor_step,toroidal_res-1):

            x = wall_contour[0,0]*np.cos(phi)
            y = wall_contour[0,0]*np.sin(phi)
            points.InsertNextPoint(x,y,wall_contour[0,1])
            npoints = npoints + 1

            for i in range(1,wall_contour.shape[0]):

                x = wall_contour[i,0]*np.cos(phi)
                y = wall_contour[i,0]*np.sin(phi)
                points.InsertNextPoint(x,y,wall_contour[i,1])
                npoints = npoints + 1
                lasttor = npoints - wall_contour.shape[0] - 1

                polygon = vtk.vtkPolygon()
                polygon.GetPointIds().SetNumberOfIds(3)
                polygon.GetPointIds().SetId(0,lasttor-1)
                polygon.GetPointIds().SetId(1,lasttor)
                polygon.GetPointIds().SetId(2,npoints-1)
                polygons.InsertNextCell(polygon)
                polygon = vtk.vtkPolygon()
                polygon.GetPointIds().SetNumberOfIds(3)
                polygon.GetPointIds().SetId(0,npoints-2)
                polygon.GetPointIds().SetId(1,npoints-1)
                polygon.GetPointIds().SetId(2,lasttor-1)
                polygons.InsertNextCell(polygon)

            # Close the end (poloidally)
            polygon = vtk.vtkPolygon()
            polygon.GetPointIds().SetNumberOfIds(3)
            polygon.GetPointIds().SetId(0,npoints-2*wall_contour.shape[0])
            polygon.GetPointIds().SetId(1,npoints-wall_contour.shape[0]-1)
            polygon.GetPointIds().SetId(2,npoints-1)
            polygons.InsertNextCell(polygon)
            polygon = vtk.vtkPolygon()
            polygon.GetPointIds().SetNumberOfIds(3)
            polygon.GetPointIds().SetId(0,npoints-wall_contour.shape[0])
            polygon.GetPointIds().SetId(1,npoints-1)
            polygon.GetPointIds().SetId(2,npoints-2*wall_contour.shape[0])
            polygons.InsertNextCell(polygon)
        
        # Close the end (toroidally)
        startpoint = npoints - wall_contour.shape[0]
        for i in range(1,wall_contour.shape[0]):
            polygon = vtk.vtkPolygon()
            polygon.GetPointIds().SetNumberOfIds(3)
            polygon.GetPointIds().SetId(0,startpoint+i-1)
            polygon.GetPointIds().SetId(1,startpoint+i)
            polygon.GetPointIds().SetId(2,i)
            polygons.InsertNextCell(polygon)
            polygon = vtk.vtkPolygon()
            polygon.GetPointIds().SetNumberOfIds(3)
            polygon.GetPointIds().SetId(0,i-1)
            polygon.GetPointIds().SetId(1,i)
            polygon.GetPointIds().SetId(2,startpoint+i-1)
            polygons.InsertNextCell(polygon)
        

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetPolys(polygons)

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)

    return actor



# Return VTK image actor and VTK image reiszer objects for this image.
def get_image_actor(image_array,clim=None,actortype='vtkImageActor'):

    image = image_array.copy()

    if clim is None:
        if image.min() != image.max():
            clim = [image.min(),image.max()]
        else:
            clim = [0,255]

    clim = np.array(clim)

    # If the array isn't already 8-bit int, make it 8-bit int...
    if image.dtype != np.uint8:
        # If we're given a higher bit-depth integer, it's easy to downcast it.
        if image.dtype == np.uint16 or image.dtype == np.int16:
            image = np.uint8(image/2**8)
            clim = np.uint8(clim/2**8)
        elif image.dtype == np.uint32 or image.dtype == np.int32:
            image = np.uint8(image/2**24)
            clim = np.uint8(clim/2**24)
        elif image.dtype == np.uint64 or image.dtype == np.int64:
            image = np.uint8(image/2**56)
            clim = np.uint8(clim/2**24)
        # Otherwise, scale it in a floating point way to its own max & min
        # and strip out any transparency info (since we can't be sure of the scale used for transparency)
        else:

            if image.min() < 0:
                image = image - image.min()
                clim = clim - image.min()

            if len(image.shape) == 3:
                if image.shape[2] == 4:
                    image = image[:,:,:-1]

            image = np.uint8(255.*(image - image.min())/(image.max() - image.min()))


    vtk_im_importer = vtk.vtkImageImport()
    
    # Create a temporary floating point copy of the data, for scaling.
    # Also flip the data (vtk addresses y from bottom right) and put in display coords.
    image = np.float32(np.flipud(image))
    clim = np.float32(clim)

    # Scale to colour limits and convert to uint8 for displaying.
    image -= clim[0]
    image /= (clim[1]-clim[0])
    image *= 255.
    image = np.uint8(image)

    im_data_string = image.tostring()
    vtk_im_importer.CopyImportVoidPointer(im_data_string,len(im_data_string))
    vtk_im_importer.SetDataScalarTypeToUnsignedChar()

    if len(image.shape) == 2:
        vtk_im_importer.SetNumberOfScalarComponents(1)
    else:
        vtk_im_importer.SetNumberOfScalarComponents(image.shape[2])

    vtk_im_importer.SetDataExtent(0,image.shape[1]-1,0,image.shape[0]-1,0,0)
    vtk_im_importer.SetWholeExtent(0,image.shape[1]-1,0,image.shape[0]-1,0,0)

    if actortype == 'vtkImageActor':
        actor = vtk.vtkImageActor()
        actor.InterpolateOff()
        mapper = actor.GetMapper()
        mapper.SetInputConnection(vtk_im_importer.GetOutputPort())

        return actor

    elif actortype == 'vtkActor2D':
        resizer = vtk.vtkImageResize()
        resizer.SetInputConnection(vtk_im_importer.GetOutputPort())
        resizer.SetResizeMethodToOutputDimensions()
        resizer.SetOutputDimensions((image_array.shape[1],image_array.shape[0],1))
        resizer.InterpolateOff()

        mapper = vtk.vtkImageMapper()
        mapper.SetInputConnection(resizer.GetOutputPort())
        mapper.SetColorWindow(255)
        mapper.SetColorLevel(127.5)

        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetDisplayLocationToForeground()

        return actor,resizer





# Get a VTK actor of 3D lines based on a set of 3D point coordinates.
def get_lines_actor(coords):

    points = vtk.vtkPoints()
    lines = vtk.vtkCellArray()
    point_ind = -1
    if coords.shape[1] == 6:

        for lineseg in range(coords.shape[0]):
            points.InsertNextPoint(coords[lineseg,:3])
            points.InsertNextPoint(coords[lineseg,3:])
            point_ind = point_ind + 2
            line = vtk.vtkLine()
            line.GetPointIds().SetId(0,point_ind - 1)
            line.GetPointIds().SetId(1,point_ind)
            lines.InsertNextCell(line)

    elif coords.shape[1] == 3:

        for pointind in range(coords.shape[0]):

            points.InsertNextPoint(coords[pointind,:])
            point_ind = point_ind + 1

            if point_ind > 0:
                line = vtk.vtkLine()
                line.GetPointIds().SetId(0,point_ind - 1)
                line.GetPointIds().SetId(1,point_ind)
                lines.InsertNextCell(line)

    polydata = polydata = vtk.vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetLines(lines)

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)

    actor.GetProperty().SetLineWidth(2)

    return actor


def render_unfolded_wall(cadmodel,calibrations=[],w=None,theta_start=90,phi_start=0,progress_callback=LoopProgPrinter().update,cancel=False,theta_steps=64,phi_steps=256,filename=None):
    """
    Render an image of the tokamak wall "flattened" out. Creates an image where the horizontal direction is the toroidal direction and
    vertical direction is poloidal.

    Parameters:

        cadmodel (calcam.CADModel)                  : CAD Model to render. The CAD model must have an R, Z wall contour embedded in it (this is \
                                                      ued in the wall flattening calculations), which can be added in the CAD model editor.
        calibrations (list of calcam.Calibration)   : List of camera calibrations to visualise on the wall. If provided, each camera calibration \
                                                      will be shown on the image as a colour shaded area on the wall indicating which parts of the wall \
                                                      the camera can see.
        w (int)                                     : Desired approximate width of the rendered image in pixels. If not given, the image width will be chosen \
                                                      to give a scale of about 2mm/pixel.
        theta_start (float)                         : Poloidal angle in degrees to "split" the image i.e. this angle will be at the top and bottom of the image. \
                                                      0 corresponds to the outboard midplane. Default is 90 degrees i.e. the top of the machine.
        phi_start (float)                           : Toroidal angle in degrees to "split" the image i.e. this angle will be at the left and right of the image.
        progress_callback (callable)                : Used for GUI integration - a callable which will be called with the fraction of the render completed. \
                                                      Default is to print an estimate of how long the render will take.
        cancel (ref to bool)                        : Used for GUI integration - a booleam which starts False, and if set to True during the calculation, the function \
                                                      will stop and return.
        theta_steps (int)                           : Number of tiles to use in the poloidal direction. The default is optimised for image quality so it is advised not \
                                                      to change this. Effects the calculation time linearly.
        phi_steps (int)                             : Number of tiles to use in the toroidal direction. The default is optimised for image quality so it is advised not \
                                                      to change this. Effects the calculation time linearly.
        filename (string)                           : If provided, the result will be saved to an image file with this name in addition to being returned as an array. \
                                                      Must include file extension.
    Returns:

        A NumPy array of size ( h * w * 3 ) and dtype uint8 containing the RGB image result.
    """
    if cadmodel.wall_contour is None:
        raise Exception('This CAD model does not have a wall contour included. This function can only be used with CAD models which have wall contours.')

    cal_actors = []
    if len(calibrations) > 0:
        ccycle = ColourCycle()
        for calib in calibrations:
            actor = get_wall_coverage_actor(calib,cadmodel,clearance=1e-2)
            actor.GetProperty().SetOpacity(0.75)
            actor.GetProperty().SetColor(next(ccycle))
            cal_actors.append(actor)


    # The camera will be placed in the centre of the R, Z wall contour
    r_min = cadmodel.wall_contour[:,0].min()
    r_max = cadmodel.wall_contour[:,0].max()
    z_min = cadmodel.wall_contour[:,1].min()
    z_max = cadmodel.wall_contour[:,1].max()
    r_mid = (r_max + r_min) / 2
    z_mid = (z_max + z_min) / 2

    # Calculate image width for ~2mm / pixel if none is given
    if w is None:
        tor_len = 2*np.pi*r_mid
        w = int(tor_len/2e-3)

    # Maximum line length which will be used for wall contour intersection tests.
    linelength = np.sqrt((r_max - r_min)**2 + (z_max - z_min)**2)

    # Theta and phi steps in radians
    theta_step = 2*np.pi/theta_steps
    phi_step = 2*np.pi/phi_steps

    # Convert starting angles to radians
    theta_start = theta_start / 180 * np.pi
    phi_start = phi_start / 180 * np.pi

    # Set up a VTK off screen window to do the image rendering
    renwin = vtk.vtkRenderWindow()
    renwin.OffScreenRenderingOn()
    renwin.SetBorders(0)
    renderer = vtk.vtkRenderer()
    renwin.AddRenderer(renderer)
    camera = renderer.GetActiveCamera()
    camera.SetViewAngle(theta_step/np.pi * 180)
    renderer.Render()
    light = renderer.GetLights().GetItemAsObject(0)
    light.SetPositional(False)

    # Width of each tile in the horizontal direction
    xsz = int(w/phi_steps)
    w = phi_steps * xsz

    cadmodel.add_to_renderer(renderer)

    for actor in cal_actors:
        renderer.AddActor(actor)

    # Initialise an array for the output image.
    out_im = np.empty((0,w,3),dtype=np.uint8)

    # Update status callback if present
    if callable(progress_callback):
        try:
            progress_callback('[render_wall] Rendering wall image')
        except Exception:
            pass
        progress_callback(0.)
        n = 0

    # Outer loop over poloidal angle
    for theta in np.linspace(theta_start + theta_step/2,theta_start + 2*np.pi - theta_step/2,theta_steps):

        # See where a line from the camera position at this poloidal angle hits the wall contour
        line_end = [linelength*np.cos(theta) + r_mid,linelength*np.sin(theta)+z_mid]
        rtarg,ztarg = get_contour_intersection(cadmodel.wall_contour,[r_mid,z_mid],line_end)

        # What aspect ratio the render window needs to be to cover the correct poloidal and toroidal angles
        r = np.sqrt((rtarg - r_mid)**2 + (ztarg - z_mid)**2)
        xangle = phi_step * rtarg / r
        aspect = theta_step / xangle
        ysz = int(xsz * aspect)

        # Camera upvec
        view_up_rz = [np.cos(theta + np.pi/2), np.sin(theta + np.pi/2)]

        # Figure out what height this strip of image should be by seeing how much distance along the wall contour
        # is included in this view angle, and maintain the same scale to physical distance around the whole wall.
        theta_low = theta - theta_step/2
        line_end = [linelength * np.cos(theta_low) + r_mid, linelength * np.sin(theta_low) + z_mid]
        r1, z1 = get_contour_intersection(cadmodel.wall_contour, [r_mid, z_mid], line_end)
        theta_up = theta + theta_step/2
        line_end = [linelength * np.cos(theta_up) + r_mid, linelength * np.sin(theta_up) + z_mid]
        r2, z2 = get_contour_intersection(cadmodel.wall_contour, [r_mid, z_mid], line_end)
        dr = np.sqrt((r2 - r1)**2 + (z2 - z1)**2)
        height = int(dr/(r_mid*2*np.pi/w))

        # This will be our horizontal strip of image corresponding to this poloidal angle
        row = np.empty((height, 0, 3), dtype=np.uint8)

        # Set the render window size
        renwin.SetSize(xsz,ysz)

        # Inner loop is over toroidal angle
        for phi in np.linspace(phi_start + phi_step/2,phi_start + 2*np.pi - phi_step/2,phi_steps):

            # Shuffle the camera and light around toroidally and point them at the right place on the wall
            camera.SetPosition(r_mid*np.cos(phi),r_mid*np.sin(phi),z_mid)
            light.SetPosition(r_mid*np.cos(phi),r_mid*np.sin(phi),z_mid)
            upvec = [np.cos(phi)*view_up_rz[0],np.sin(phi)*view_up_rz[0],view_up_rz[1]]
            camera.SetViewUp(upvec)
            camera.SetFocalPoint(rtarg*np.cos(phi),rtarg*np.sin(phi),ztarg)
            light.SetFocalPoint(rtarg*np.cos(phi),rtarg*np.sin(phi),ztarg)

            # Render the image tile
            renwin.Render()
            vtk_win_im = vtk.vtkWindowToImageFilter()
            vtk_win_im.SetInput(renwin)
            vtk_win_im.Update()
            vtk_image = vtk_win_im.GetOutput()
            vtk_array = vtk_image.GetPointData().GetScalars()
            dims = vtk_image.GetDimensions()
            im = np.flipud(vtk_to_numpy(vtk_array).reshape(dims[1], dims[0] , 3))

            # Squash or stretch it vertically to get the poloidal scale right, as calculated earlier.
            im = cv2.resize(im,(xsz,height))

            # Glue it on to the output
            row = np.hstack((im,row))

            # Update the status callback, if present
            if callable(progress_callback):
                n += 1
                progress_callback(n/(theta_steps*phi_steps))

            # Stop if cancellation has been requested
            if cancel:
                break

        # Stick this image strip on to the output.
        out_im = np.vstack((row,out_im))

        if cancel:
            break

    if callable(progress_callback):
        progress_callback(1.)

    cadmodel.remove_from_renderer(renderer)
    renwin.Finalize()

    # Save the image if given a filename
    if filename is not None:

        # Re-shuffle the colour channels for saving (openCV needs BGR / BGRA)
        save_im = copy.copy(out_im)
        save_im[:,:,:3] = save_im[:,:,2::-1]
        cv2.imwrite(filename,save_im)
        print('[render_wall] Result saved as {:s}'.format(filename))

    return out_im