"""
Line-intersection-based fitting.

Contact: Tino Michael <Tino.Michael@cea.fr>
"""


from ctapipe.reco.reco_algorithms import Reconstructor
from ctapipe.io.containers import ReconstructedShowerContainer
from ctapipe.coordinates import GroundFrame, TiltedGroundFrame, HorizonFrame, CameraFrame
from astropy.coordinates import SkyCoord, spherical_to_cartesian, cartesian_to_spherical
from itertools import combinations

import numpy as np

from scipy.optimize import minimize

from astropy import units as u


__all__ = ['HillasReconstructor',
           'TooFewTelescopes',
           'dist_to_traces', 'MEst', 'GreatCircle']


class TooFewTelescopesException(Exception):
    pass


def angle(v1, v2):
    """ computes the angle between two vectors
        assuming carthesian coordinates

    Parameters
    ----------
    v1 : numpy array
    v2 : numpy array

    Returns
    -------
    the angle between v1 and v2 as a dimensioned astropy quantity
    """
    return np.arccos(np.clip(v1.dot(v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)), -1.0, 1.0))


def normalise(vec):
    """ Sets the length of the vector to 1
        without changing its direction

    Parameters
    ----------
    vec : numpy array

    Returns
    -------
    numpy array with the same direction but length of 1
    """
    try:
        return vec / np.linalg.norm(vec)
    except ZeroDivisionError:
        return vec


def dist_to_traces(core, circles):
    """This function calculates the M-Estimator from the distances of the
    suggested core position to all traces of the given GreatCircles.
    The distance of the core to the trace line is the length of the
    vector between the core and an arbitrary point on the trace
    projected perpendicular to the trace.

    This is implemented as the scalar product of the connecting vector
    between the core and the position of the telescope and { trace[1],
    -trace[0] } as the normal vector of the trace.

    Notes
    -----
    uses the M-Estimator of the distance instead of the distance itself:

    .. math::

        M_\\text{Est} = \sum_i{ 2 \cdot \sqrt{1 + d_i^2} - 2}


    """

    mest = 0.
    for circle in circles.values():

        # the distanece of the core
        D = core - circle.pos[:2] / u.m
        dist = D[0] * circle.trace[1] - D[1] * circle.trace[0]

        # summing up the M-Estimator with the given circle weights
        mest += (2 * np.sqrt(1 + (dist ** 2)) - 2) * circle.weight
    return mest


# def MEst(origin, circles, weights):
#     """calculates the M-Estimator: a modified χ² that becomes
#     asymptotically linear for high values and is therefore less
#     sensitive to outliers
#
#     the test is performed to maximise the angles between the fit
#     direction and the all the normal vectors of the great circles
#
#     .. math::
#
#         M_\\text{Est} = \sum_i{ 2 \cdot \sqrt{1 + d_i^2} - 2}
#
#
#     Notes
#     -----
#     seemingly inferior to negative sum of sin(angle)...
#
#
#     Parameters
#     -----------
#     origin : length-3 array
#         direction vector of the gamma's origin used as seed
#     circles : GreatCircle array
#         collection of great circles created from the camera images
#     weights : array
#         list of weights for each image/great circle
#
#     Returns
#     -------
#     MEstimator : float
#
#
#     """
#
#     sin_ang = np.array([np.linalg.norm(np.cross(origin, circ.norm))
#                         for circ in circles.values()])
#     return -2 * np.sum(weights * np.sqrt((1 + np.square(sin_ang))) - 2)


# def neg_angle_sum(origin, circles, weights):
#     """calculates the negative sum of the angle between the fit direction
#     and all the normal vectors of the great circles
#
#     Parameters
#     -----------
#     origin : length-3 array
#         direction vector of the gamma's origin used as seed
#     circles : GreatCircle array
#         collection of great circles created from the camera images
#     weights : array
#         list of weights for each image/great circle
#
#     Returns
#     --------
#     n_sum_angles : float
#         negative of the sum of the angles between the test direction
#         and all normal vectors of the given great circles
#
#     """
#
#     sin_ang = np.array([np.linalg.norm(np.cross(origin, circ.norm))
#                         for circ in circles.values()])
#     return -np.sum(weights * sin_ang)


class HillasReconstructor(Reconstructor):
    """
    class that reconstructs the direction of an atmospheric shower
    using a simple hillas parametrisation of the camera images it
    provides a direction estimate in two steps and an estimate for the
    shower's impact position on the ground.

    so far, it does neither provide an energy estimator nor an
    uncertainty on the reconstructed parameters

    """

    def __init__(self, config=None, tool=None, **kwargs):
        super().__init__(config=config, parent=tool, **kwargs)
        self.circles = {}

    def predict(self, hillas_dict, inst, pointing_alt, pointing_az, seed_pos=(0, 0)):
        """The function you want to call for the reconstruction of the
        event. It takes care of setting up the event and consecutively
        calls the functions for the direction and core position
        reconstruction.  Shower parameters not reconstructed by this
        class are set to np.nan

        Parameters
        -----------
        hillas_dict : python dictionary
            dictionary with telescope IDs as key and
            MomentParameters instances as values
        inst : ctapipe.io.InstrumentContainer
            instrumental description
        pointing_alt:
        pointing_az:
        seed_pos : python tuple
            shape (2) tuple with a possible seed for
            the core position fit (e.g. CoG of all telescope images)

        Raises
        ------
        TooFewTelescopesException
            if len(hillas_dict) < 2

        """

        # stereoscopy needs at least two telescopes
        if len(hillas_dict) < 2:
            raise TooFewTelescopesException(
                "need at least two telescopes, have {}"
                .format(len(hillas_dict)))

        self.inititialize_great_circles(hillas_dict, inst.subarray, pointing_alt, pointing_az)

        # algebraic direction estimate
        direction, err_est_dir = self.fit_origin_crosses()

        # core position estimate using a geometric approach
        pos, err_est_pos = self.fit_core_crosses()

        # container class for reconstructed showers
        result = ReconstructedShowerContainer()
        r, lat, lon = cartesian_to_spherical(*direction)


        result.alt, result.az = lat, lon
        result.core_x = pos[0]
        result.core_y = pos[1]
        result.core_uncert = err_est_pos

        result.tel_ids = [h for h in hillas_dict.keys()]
        result.average_size = np.mean([h.size for h in hillas_dict.values()])
        result.is_valid = True

        result.alt_uncert = err_est_dir
        result.az_uncert = np.nan
        # result.h_max = self.fit_h_max(hillas_dict, inst.subarray, pointing_alt, pointing_az)
        result.h_max = np.nan
        result.h_max_uncert = np.nan
        result.goodness_of_fit = np.nan

        return result

    def inititialize_great_circles(self, hillas_dict, subarray, pointing_alt, pointing_az):
        """
        creates a dictionary of :class:`.GreatCircle` from a dictionary of
        hillas
        parameters

        Parameters
        ----------
        hillas_dict : dictionary
            dictionary of hillas moments
        subarray : ctapipe.instrument.SubarrayDescription
            subarray information
        tel_phi, tel_theta : dictionaries
            dictionaries of the orientation angles of the telescopes
            needs to contain at least the same keys as in `hillas_dict`
        """

        self.circles = {}
        for tel_id, moments in hillas_dict.items():
            p2_x = moments.cen_x + 0.1 * u.m * np.cos(moments.psi)
            p2_y = moments.cen_y + 0.1 * u.m * np.sin(moments.psi)
            focal_length = subarray.tel[tel_id].optics.equivalent_focal_length


            cog_coord = CameraFrame(moments.cen_x, moments.cen_y, focal_length=focal_length)
            pointing = SkyCoord(alt=pointing_alt[tel_id], az=pointing_az[tel_id], frame='altaz')

            hf = HorizonFrame(array_direction=pointing, pointing_direction=pointing)
            cf = CameraFrame(focal_length=focal_length, array_direction=pointing, pointing_direction=pointing)

            cog_coord = SkyCoord(x=moments.cen_x, y=moments.cen_y, frame=cf)
            p2_coord = SkyCoord(x=p2_x, y=p2_y, frame=cf)

            cog_coord = cog_coord.transform_to(hf)
            p2_coord = p2_coord.transform_to(hf)
            circle = GreatCircle(
                a=np.array(spherical_to_cartesian(1, cog_coord.alt, cog_coord.az)).ravel(),
                b=np.array(spherical_to_cartesian(1, p2_coord.alt, p2_coord.az)).ravel(),
                weight=moments.size * (moments.length / moments.width)
            )
            circle.pos = subarray.positions[tel_id]
            self.circles[tel_id] = circle

    def fit_origin_crosses(self):
        """calculates the origin of the gamma as the weighted average
        direction of the intersections of all great circles

        Returns
        -------
        gamma : shape (3) numpy array
            direction of origin of the reconstructed shower as a 3D vector
        crossings : shape (n,3) list
            list of all the crossings of the `GreatCircle` list
        """

        crossings = []
        for perm in combinations(self.circles.values(), 2):
            n1, n2 = perm[0].norm, perm[1].norm
            # cross product automatically weighs in the angle between
            # the two vectors: narrower angles have less impact,
            # perpendicular vectors have the most
            crossing = np.cross(n1, n2)

            # two great circles cross each other twice (one would be
            # the origin, the other one the direction of the gamma) it
            # doesn't matter which we pick but it should at least be
            # consistent: make sure to always take the "upper" solution
            if crossing[2] < 0:
                crossing *= -1
            crossings.append(crossing * perm[0].weight * perm[1].weight)

        result = normalise(np.sum(crossings, axis=0))
        off_angles = [angle(result, cross) for cross in crossings] * u.rad

        err_est_dir = np.average(
            off_angles,
            weights=[len(cross) for cross in crossings]
        )

        # averaging over the solutions of all permutations
        return result, err_est_dir



    def fit_core_crosses(self):
        r"""calculates the core position as the least linear square solution
        of an (over-constrained) equation system

        Notes
        -----
        The basis is the "trace" of each telescope's `GreatCircle` which
        can be determined by the telescope's position P=(Px, Py) and
        the circle's normal vector, projected to the ground n=(nx,
        ny), so that for every r=(x, y) on the trace

        :math:`\vec n \cdot \vec r = \vec n \cdot \vec P` ,

        :math:`n_x \cdot x + n_y \cdot y = d`

        In a perfect world, the traces of all telescopes cross in the
        shower's point of impact. This means that there is one common
        point (x, y) for every telescope, so we can write in matrix
        form:

        .. math::
            :label: fullmatrix

            \begin{pmatrix}
                nx_1  &  ny_1  \\
                \vdots & \vdots \\
                nx_n  &  ny_n
            \end{pmatrix}
                \cdot (x, y) =
            \begin{pmatrix}
                d_1  \\
                \vdots \\
                d_n
            \end{pmatrix}



        or :math:`\boldsymbol{A} \cdot \vec r = \vec D` .

        Since we do not live in a perfect world and there probably is
        no point r that fulfils this equation system, it is solved by
        the method of least linear square:

        .. math::
            :label: rchisqr

            \vec{r}_{\chi^2} = (\boldsymbol{A}^\text{T} \cdot \boldsymbol{A})^{-1}
            \boldsymbol{A}^\text{T} \cdot \vec D


        :math:`\vec{r}_{\chi^2}` minimises the squared difference of


        .. math::

            \vec D - \boldsymbol{A} \cdot \vec r


        Weights are applied to every line of equation :eq:`fullmatrix`
        as stored in circle.weight (assuming they have been set in
        `get_great_circles` or elsewhere).

        Returns
        -------
        r_chisqr: numpy.ndarray(2)
            the minimum :math:`\chi^2` solution for the shower impact position
        pos_uncert: astropy length quantity
            error estimate on the reconstructed core position

        """

        A = np.zeros((len(self.circles), 2))
        D = np.zeros(len(self.circles))
        for i, circle in enumerate(self.circles.values()):
            # apply weight from circle and from the tilt of the circle
            # towards the horizontal plane: simply projecting
            # circle.norm to the ground gives higher weight to planes
            # perpendicular to the ground and less to those that have
            # a steeper angle
            A[i] = circle.weight * circle.norm[:2]
            # since A[i] is used in the dot-product, no need to multiply the
            # weight here
            D[i] = np.dot(A[i], circle.pos[:2])

        # the math from equation (2) would look like this:
        # ATA = np.dot(A.T, A)
        # ATAinv = np.linalg.inv(ATA)
        # ATAinvAT = np.dot(ATAinv, A.T)
        # return np.dot(ATAinvAT, D) * unit

        # instead used directly the numpy implementation
        # speed is the same, just handles already "SingularMatrixError"
        pos = np.linalg.lstsq(A, D)[0] * u.m

        weighted_sum_dist = np.sum([np.dot(pos[:2] - c.pos[:2], c.norm[:2]) * c.weight
                                    for c in self.circles.values()]) * pos.unit
        norm_sum_dist = np.sum([c.weight * np.linalg.norm(c.norm[:2])
                                for c in self.circles.values()])
        pos_uncert = abs(weighted_sum_dist / norm_sum_dist)

        return pos, pos_uncert

    # def fit_core_minimise(self, seed=(0, 0), test_function=dist_to_traces):
    #     """
    #     reconstructs the shower core position from the already set up
    #     great circles
    #
    #     Notes
    #     -----
    #     The core of the shower lies on the cross section of the great
    #     circle with the horizontal plane. The direction of this cross
    #     section is the cross-product of the circle's normal vector and
    #     the horizontal plane.  Here, we only care about the direction;
    #     not the orientation...
    #
    #
    #     Parameters
    #     ----------
    #     seed : tuple
    #         shape (2) tuple with optional starting coordinates
    #         tuple of floats or astropy.length -- if floats, assume metres
    #     test_function : function object, optional (default: dist_to_traces)
    #         function to be used by the minimiser
    #
    #     """
    #
    #     if type(seed) == u.Quantity:
    #         unit = seed.unit
    #     else:
    #         unit = u.m
    #
    #     # the direction of the cross section of the great circle with
    #     # the horizontal frame is the cross product of the great
    #     # circle's normal vector with the z-axis:
    #     # n × z = (n[1], -n[0], 0)
    #     for circle in self.circles.values():
    #         circle.trace = normalise(np.array([circle.norm[1],
    #                                                   -circle.norm[0], 0]))
    #
    #     # minimising the test function (note: minimize strips seed off its
    #     # unit)
    #     self.fit_result_core = minimize(test_function, seed[:2],
    #                                     args=(self.circles),
    #                                     method='BFGS', options={'disp': False})
    #
    #     if not self.fit_result_core.success:
    #         print("fit_core: fit no success")
    #
    #     return np.array(self.fit_result_core.x) * unit

    def fit_h_max(self, hillas_dict, subarray, tel_phi, tel_theta):

        weights = []
        tels = []
        dirs = []
        for tel_id, hillas in hillas_dict.items():
            foclen = subarray.tel[tel_id].optics.equivalent_focal_length
            max_dir, = trafo.pixel_position_to_direction(
                np.array([hillas.cen_x / u.m]) * u.m,
                np.array([hillas.cen_y / u.m]) * u.m,
                tel_phi[tel_id], tel_theta[tel_id], foclen)
            weights.append(self.circles[tel_id].weight)
            tels.append(self.circles[tel_id].pos)
            dirs.append(max_dir)

        # minimising the test function
        pos_max = minimize(dist_to_line3d, np.array([0, 0, 10000]),
                           args=(np.array(tels), np.array(dirs), np.array(weights)),
                           method='BFGS',
                           options={'disp': False}
                           ).x
        return pos_max[2] * u.m


def dist_to_line3d(pos, tels, dirs, weights):
    result = np.average(np.linalg.norm(np.cross((pos - tels), dirs), axis=1),
                        weights=weights)
    return result


class GreatCircle:
    """
    a tiny helper class to collect some parameters for each great great
    circle
    """

    def __init__(self, a, b, weight=1):
        """The constructor takes two direction vectors which define a plane perpedicular
        to the camera.

        Parameters
        -----------
        a: numpy.ndarray(3)
            One of two direction vectors which define the plane.
            These vectors are eucledian [x, y, z] where positive z values point towards the sky
            and x and y are parallel to the ground.
        weight : float, optional
            weight of this plane for later use during the reconstruction

        Notes
        -----
        c: numpy.ndarray(3)
            :math:`\vec c = (\vec a \times \vec b) \times \vec a`
            :math:`\rightarrow` a and c form an orthogonal base for the
            great circle
            (only orthonormal if a and b are of unit-length)
        norm: numpy.ndarray(3)
            normal vector of the circle's plane,
            perpendicular to a, b and c
        """

        self.a = a
        self.b = b

        # a and c form an orthogonal basis for the great circle
        # not really necessary since the norm can be calculated
        # with a and b just as well
        self.c = np.cross(np.cross(self.a, self.b), self.a)
        # normal vector for the plane defined by the great circle
        self.norm = normalise(np.cross(self.a, self.c))
        # some weight for this circle
        # (put e.g. uncertainty on the Hillas parameters
        # or number of PE in here)
        self.weight = weight
