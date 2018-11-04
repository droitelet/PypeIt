# Module for guiding Slit/Order tracing
from __future__ import absolute_import, division, print_function

import inspect
import numpy as np
import os
from subprocess import Popen

from scipy import ndimage

from importlib import reload

from astropy.io import fits

from linetools import utils as ltu

from pypeit import msgs
from pypeit import debugger
from pypeit.core import pixels
from pypeit.core import trace_slits
from pypeit import utils
from pypeit import masterframe
from pypeit import ginga
from pypeit import traceimage

from pypeit.par import pypeitpar
from pypeit.par.util import parset_to_dict


class TraceSlits(masterframe.MasterFrame):
    """Class to guide slit/order tracing

    Parameters
    ----------
    mstrace : ndarray
      Trace image
    pixlocn : ndarray
      Pixel location array
    binbpx : ndarray, optional
      Bad pixel mask
      If not provided, a dummy array with no masking is generated
    settings : dict, optional
      Settings for trace slits
    det : int, optional
      Detector number
    ednum : int, optional
      Edge number used for indexing
    redux_path : str, optional
      Used for QA output

    Attributes
    ----------
    frametype : str
      Hard-coded to 'trace'
    lcen : ndarray [nrow, nslit]
      Left edges, in physical space
    rcen : ndarray [nrow, nslit]
      Right edges, in physical space
    lordpix : ndarray [nrow, nslit]
      Left edges, in pixel space
    rordpix : ndarray [nrow, nslit]
      Right edges, in pixel space
    slitpix : ndarray (int)
      Image specifiying which pixels are in which slit
    pixcen : ndarray [nrow, nslit]
      Pixel values down the center of the slit
    pixwid : ndarray [nrow, nslit]
      Width of slit in pixels (integer)
    extrapord : ndarray
      ??

    edgearr : ndarray
      Edge image
      -200000, 200000 indexing -- ??
      -100000, 100000 indexing -- Edges defined but additional work in progress
      -1, 1 indexing -- Edges finalized
    tc_dict : dict, optional
      Dict guiding multi-slit work
      [left,right][xval][edge]
    steps : list
      List of the processing steps performed
    siglev : ndarray
      Sobolev filtered image of mstrace
      Used to find images and used for tracing
    binarr : ndarray
      Uniform filter version of mstrace
      Generated by make_binarr()
    user_set : bool
      Did the user set the slit?  If so, most of the automated algorithms are skipped
    lmin : int
      Lowest left edge, edgearr value
    lmax : int
      Highest left edge, edgearr value
    rmin : int
      Lowest right edge, edgearr value
    rmax : int
      Highest right edge, edgearr value
    lcnt : int
      Number of left edges
    rcnt : int
      Number of right edges
    """

    # Frametype is a class attribute
    frametype = 'trace'

    def __init__(self, mstrace, pixlocn, par=None, det=None, setup=None, master_dir=None,
                 redux_path=None,
                 mode=None, binbpx=None, ednum=100000):

        # MasterFrame
        masterframe.MasterFrame.__init__(self, self.frametype, setup, master_dir=master_dir, mode=mode)

        # TODO -- Remove pixlocn as a required item

        # TODO: (KBW) Why was setup='' in this argument list and
        # setup=None in all the others?  Is it because of the
        # from_master_files() classmethod below?  Changed it to match
        # the rest of the MasterFrame children.

        # Required parameters (but can be None)
        self.mstrace = mstrace
        self.pixlocn = pixlocn

        # Set the parameters, using the defaults if none are provided
        self.par = pypeitpar.TraceSlitsPar() if par is None else par

        # Optional parameters
        self.redux_path = redux_path
        self.det = det
        self.ednum = ednum
        if binbpx is None: # Bad pixel array
            self.binbpx = np.zeros_like(mstrace)
            self.input_binbpx = False # For writing
        else:
            self.binbpx = binbpx
            self.input_binbpx = True

        # Main outputs
        self.lcen = None     # narray
        self.rcen = None     # narray
        self.tc_dict = None  # dict
        self.edgearr = None  # ndarray
        self.siglev = None   # ndarray
        self.steps = []
        self.extrapord = None
        self.pixcen = None
        self.pixwid = None
        self.lordpix = None
        self.rordpix = None
        self.slitpix = None
        self.lcen_tweak = None   # Place holder for tweaked slit boundaries from flat fielding routine.
        self.rcen_tweak = None

        # Key Internals
        if mstrace is not None:
            self.binarr = self.make_binarr()
        self.user_set = None
        self.lmin = None
        self.lmax = None
        self.rmin = None
        self.rmax = None
        self.lcnt = None
        self.rcnt = None
        self.lcoeff = None
        # Fitting
        self.lnmbrarr = None
        self.ldiffarr = None
        self.lwghtarr = None
        self.rcoeff = None
        self.rnmbrarr = None
        self.rdiffarr = None
        self.rwghtarr = None

    @classmethod
    def from_master_files(cls, root, load_pix_obj=False):
        """
        Instantiate from the primary MasterFrame outputs of the class

        Parameters
        ----------
        root : str
          Path + root name for the TraceSlits objects (FITS, JSON)

        Returns
        -------
        slf

        """
        fits_dict, ts_dict = load_traceslit_files(root)
        msgs.info("Loading Slits from {:s}".format(root + '.fits.gz'))


        # Deal with the bad pixel image
        if 'BINBPX' in fits_dict.keys():
            binbpx = fits_dict['BINBPX'].astype(float)
            msgs.info("Loading BPM from {:s}".format(root+'.fits.gz'))
        else:
            binbpx = None

        # Instantiate from file
        slf = cls(fits_dict['MSTRACE'], fits_dict['PIXLOCN'], binbpx=binbpx,
                  par=pypeitpar.TraceSlitsPar.from_dict(ts_dict['settings']))

        # Fill in a bit more (Attributes)
        slf.steps = ts_dict['steps']

        # Others
        for key in ['LCEN', 'RCEN', 'EDGEARR', 'SIGLEV']:
            if key in fits_dict.keys():
                setattr(slf, key.lower(), fits_dict[key])
        # dict
        slf.tc_dict = ts_dict['tc_dict']

        # Load the pixel objects?
        if load_pix_obj:
            slf._make_pixel_arrays()

        # Return
        return slf

    @property
    def nslit(self):
        if self.lcen is None:
            return 0
        else:
            return self.lcen.shape[1]

    def make_binarr(self):
        """
        Lightly process mstrace

        Returns
        -------
        binarr : ndarray

        """
        #  Only filter in the spectral dimension, not spatial!
        self.binarr = ndimage.uniform_filter(self.mstrace, size=(3, 1), mode='mirror')
        # Step
        self.steps.append(inspect.stack()[0][3])
        return self.binarr

    def _edgearr_from_binarr(self):
        """
        Generate the first edgearr from the Sobolev produced siglev image
        Wrapper to trace_slits.edgearr_from_binarr

        Returns
        -------
        self.edgearr : ndarray (internal)
        self.siglev : ndarray (internal)

        """
        self.siglev, self.edgearr \
                = trace_slits.edgearr_from_binarr(self.binarr, self.binbpx,
                                                   medrep=self.par['medrep'],
                                                   sobel_mode=self.par['sobel_mode'],
                                                   sigdetect=self.par['sigdetect'],
                                                   number_slits=self.par['number'])
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _edgearr_single_slit(self):
        """
        Generate the first edgearr from a user-supplied single slit
        Note this is different from add_user_slits (which is handled below)

        Wrapper to trace_slits.edgearr_from_user

        Returns
        -------
        self.edgearr : ndarray (internal)
        self.siglev : ndarray (internal)

        """
        #  This trace slits single option is likely to be deprecated
        iledge, iredge = (self.det-1)*2, (self.det-1)*2+1
        self.edgearr = trace_slits.edgearr_from_user(self.mstrace.shape,
                                                      self.par['single'][iledge],
                                                      self.par['single'][iredge], self.det)
        self.siglev = None
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _add_left_right(self):
        """
        Add left/right edges to edgearr

        Wrapper to trace_slits.edgearr_add_left_right()
        If 0 is returned for both counts, this detector will be skipped

        Returns
        -------
        any_slits: bool

        self.edgearr : ndarray (internal)
        self.lcnt : int (internal)
        self.rcnt : int (internal)


        """
        self.edgearr, self.lcnt, self.rcnt = trace_slits.edgearr_add_left_right(
            self.edgearr, self.binarr, self.binbpx, self.lcnt, self.rcnt, self.ednum)
        # Check on return
        if (self.lcnt == 0) and (self.rcnt == 0):
            any_slits = False
        else:
            any_slits = True

        # Step
        self.steps.append(inspect.stack()[0][3])
        return any_slits

    def add_user_slits(self, user_slits, run_to_finish=False):
        """
        Add a user-defined slit

        Wrapper to trace_slits.add_user_edges()

        Parameters
        ----------
        user_slits : list
        run_to_finish : bool (optional)
          Perform the additional steps to complete TraceSlit operation

        Returns
        -------
        self.edgearr : ndarray (internal)

        """
        # Reset (if needed) -- For running after PYPIT took a first pass
        self.reset_edgearr_ednum()
        # Add user input slits
        self.edgearr = trace_slits.add_user_edges(self.edgearr, self.siglev, self.tc_dict, user_slits)
        # Finish
        if run_to_finish:
            self._set_lrminx()
            self._fit_edges('left')
            self._fit_edges('right')
            self._synchronize()
            self._pca()
            self._trim_slits(plate_scale = plate_scale)
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _assign_edges(self):
        """
        Assign slit edges by analyzing edgearr
        Single slits are handled trivially

        Wrapper to trace_slits.assign_slits()

        Returns
        -------
        self.edgearr : ndarray (internal)

        """

        # Assign left edges
        msgs.info("Assigning left slit edges")
        if self.lcnt == 1:
            self.edgearr[np.where(self.edgearr <= -2*self.ednum)] = -self.ednum
        else:
            trace_slits.assign_slits(self.binarr, self.edgearr, lor=-1,
                                      function=self.par['function'],
                                      polyorder=self.par['polyorder'])
        # Assign right edges
        msgs.info("Assigning right slit edges")
        if self.rcnt == 1:
            self.edgearr[np.where(self.edgearr >= 2*self.ednum)] = self.ednum
        else:
            trace_slits.assign_slits(self.binarr, self.edgearr, lor=+1,
                                      function=self.par['function'],
                                      polyorder=self.par['polyorder'])
        # Steps
        self.steps.append(inspect.stack()[0][3])

    def _chk_for_longslit(self):
        """
        Are we done?, i.e. we have a simple longslit

        Returns
        -------
        self.lcen : ndarray (internal)
        self.rcen : ndarray (internal)

        """
        #   Check if no further work is needed (i.e. there only exists one order)
        if (self.lmax+1-self.lmin == 1) and (self.rmax+1-self.rmin == 1):
            plxbin = self.pixlocn[:, :, 0].copy()
            minvf, maxvf = plxbin[0, 0], plxbin[-1, 0]
            # Just a single order has been identified (i.e. probably longslit)
            msgs.info("Only one slit was identified. Should be a longslit.")
            xint = self.pixlocn[:, 0, 0]
            # Finish
            self.lcen = np.zeros((self.mstrace.shape[0], 1))
            self.rcen = np.zeros((self.mstrace.shape[0], 1))
            self.lcen[:, 0] = utils.func_val(self.lcoeff[:, 0], xint, self.par['function'],
                                               minv=minvf, maxv=maxvf)
            self.rcen[:, 0] = utils.func_val(self.rcoeff[:, 0], xint, self.par['function'],
                                               minv=minvf, maxv=maxvf)
            return True
        else:
            return False

    def _fill_tslits_dict(self):
        """
        Build a simple object holding the key trace bits and pieces that PYPIT wants

        Returns
        -------
        self.trace_slits_dict : dict

        """
        self.tslits_dict = {}
        # Have the slit boundaries been tweaked? If so use the tweaked boundaries
        if self.lcen_tweak is not None:
            self.tslits_dict['lcen'] = self.lcen_tweak
            self.tslits_dict['rcen'] = self.rcen_tweak
        else:
            self.tslits_dict['lcen'] = self.lcen
            self.tslits_dict['rcen'] = self.rcen
        # Fill in the rest of the keys that were generated by make_pixel_arrays from the slit boundaries. This was
        # done with tweaked boundaries if they exist.
        for key in ['pixcen', 'pixwid', 'lordpix','rordpix', 'extrapord', 'slitpix', 'ximg', 'edge_mask']:
            self.tslits_dict[key] = getattr(self, key)
        return self.tslits_dict

    def _final_left_right(self):
        """
        Last check on left/right edges

        Wrapper to trace_slits.edgearr_final_left_right()

        Returns
        -------
        self.edgearr : ndarray (internal)
        self.lcnt : int (internal)
        self.rcnt : int (internal)

        """
        # Final left/right edgearr fussing (as needed)
        self.edgearr, self.lcnt, self.rcnt = trace_slits.edgearr_final_left_right(
            self.edgearr, self.ednum, self.siglev)
        # Steps
        self.steps.append(inspect.stack()[0][3])

    def _fit_edges(self, side):
        """
        Fit the edges with (left or right)

        Wrapper to trace_slits.fit_edges()

        Parameters
        ----------
        side : str
          'left' or 'right'

        Returns
        -------
        self.lcoeff
        self.lnmbrarr
        self.ldiffarr
        self.lwghtarr
        or
        self.rcoeff
        self.rnmbrarr
        self.rdiffarr
        self.rwghtarr

        """
        # Setup for fitting
        plxbin = self.pixlocn[:, :, 0].copy()
        plybin = self.pixlocn[:, :, 1].copy()

        # Fit
        if side == 'left':
            self.lcoeff, self.lnmbrarr, self.ldiffarr, self.lwghtarr \
                    = trace_slits.fit_edges(self.edgearr, self.lmin, self.lmax, plxbin, plybin,
                                             left=True, polyorder=self.par['polyorder'],
                                             function=self.par['function'])
        else:
            self.rcoeff, self.rnmbrarr, self.rdiffarr, self.rwghtarr \
                    = trace_slits.fit_edges(self.edgearr, self.rmin, self.rmax, plxbin, plybin,
                                             left=False, polyorder=self.par['polyorder'],
                                             function=self.par['function'])

        # Steps
        self.steps.append(inspect.stack()[0][3]+'_{:s}'.format(side))

    def _ignore_orders(self):
        """
        Ignore orders/slits on the edge of the detector when they run off
          Recommended for Echelle only

        Wrapper to trace_slits.edgearr_ignore_orders()

        Returns
        -------
        self.edgearr  : ndarray (internal)
        self.lmin : int (intenal)
        self.lmax: int (intenal)
        self.rmin : int (intenal)
        self.rmax: int (intenal)

        """
        self.edgearr, self.lmin, self.lmax, self.rmin, self.rmax \
                = trace_slits.edgearr_ignore_orders(self.edgearr, self.par['fracignore'])
        # Steps
        self.steps.append(inspect.stack()[0][3])

    def _make_pixel_arrays(self):
        """
        Generate pixel arrays
        Primarily for later stages of PYPIT

        Returns
        -------
        self.pixcen
        self.pixwid
        self.lordpix
        self.rordpix
        self.slitpix

        """
        if self.lcen_tweak is not None:
            msgs.info("Using tweaked slit boundaries determined from IllumFlat")
            lcen = self.lcen_tweak
            rcen = self.rcen_tweak
        else:
            lcen = self.lcen
            rcen = self.rcen
        # Convert physical traces into a pixel trace
        msgs.info("Converting physical trace locations to nearest pixel")
        self.pixcen = pixels.phys_to_pix(0.5*(lcen+rcen), self.pixlocn, 1)
        self.pixwid = (rcen-lcen).mean(0).astype(np.int)
        self.lordpix = pixels.phys_to_pix(lcen, self.pixlocn, 1)
        self.rordpix = pixels.phys_to_pix(rcen, self.pixlocn, 1)

        # Slit pixels
        msgs.info("Identifying the pixels belonging to each slit")
        self.slitpix = pixels.slit_pixels(lcen, rcen, self.mstrace.shape,self.par['pad'])
        # ximg and edge mask
        self.ximg, self.edge_mask = pixels.ximg_and_edgemask(lcen, rcen, self.slitpix,trim_edg=self.par['trim'])

    def _match_edges(self):
        """
        # Assign a number to each edge 'grouping'

        Wrapper to trace_slits.match_edges()

        Returns
        -------
        self.edgearr  : ndarray (internal)
        self.lcnt : int (intenal)
        self.rcnt: int (intenal)

        """

        self.lcnt, self.rcnt = trace_slits.match_edges(self.edgearr, self.ednum)
        # Sanity check (unlikely we will ever hit this)
        if self.lcnt >= self.ednum or self.rcnt >= self.ednum:
            msgs.error("Found more edges than allowed by ednum. Set ednum to a larger number.")
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _maxgap_prep(self):
        """
        First step in the maxgap algorithm
          Likely to be Deprecated

        Returns
        -------
        self.edgearr  : ndarray (internal)
        self.edgearrcp  : ndarray (internal)

        """
        self.edgearrcp = self.edgearr.copy()
        self.edgearr[np.where(self.edgearr < 0)] += 1 + np.max(self.edgearr) - np.min(self.edgearr)
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _maxgap_close(self):
        """
        Handle close edges (as desired by the user)
          JXP does not recommend using this method for multislit

        Wrapper to trace_slits.edgearr_close_slits()

        Returns
        -------
        self.edgearr  : ndarray (internal)

        """
        self.edgearr = trace_slits.edgearr_close_slits(self.binarr, self.edgearr, self.edgearrcp,
                                                        self.ednum, self.par['maxgap'],
                                                        function=self.par['function'],
                                                        polyorder=self.par['polyorder'])
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _mslit_sync(self):
        """
        Synchronize slits in multi-slit mode (ARMLSD)

        Wrapper to trace_slits.edgearr_mslit_sync()

        Returns
        -------
        self.edgearr  : ndarray (internal)
        self.tc_dict  : dict (internal)

        """
        #
        self.edgearr = trace_slits.edgearr_mslit_sync(self.edgearr, self.tc_dict, self.ednum)
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _mslit_tcrude(self, maxshift=0.15):
        """
        Trace crude me
          And fuss with slits

        Wrapper to trace_slits.edgearr_tcrude()

        Returns
        -------
        self.edgearr  : ndarray (internal)
        self.tc_dict  : dict (internal)

        """
        # Settings
        _maxshift = self.par['maxshift'] if 'maxshift' in self.par.keys() else maxshift

        self.edgearr, self.tc_dict = trace_slits.edgearr_tcrude(self.edgearr, self.siglev,
                                                                 self.ednum, maxshift=_maxshift)
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _pca(self):
        """
        Perform PCA analysis and extrapolation, if requested
          Otherwise move along

        Returns
        -------
        self.lcen  : ndarray (internal)
        self.rcen  : ndarray (internal)
        self.extrapord  : ndarray (internal)

        """
        if self.par['pcatype'] == 'order':
            self._pca_order_slit_edges()
        elif self.par['pcatype'] == 'pixel':
            self._pca_pixel_slit_edges()
        else: # No PCA
            allord = np.arange(self.lcent.shape[0])
            maskord = np.where((np.all(self.lcent, axis=1) == False)
                                | (np.all(self.rcent, axis=1) == False))[0]
            ww = np.where(np.in1d(allord, maskord) == False)[0]
            self.lcen = self.lcent[ww, :].T.copy()
            self.rcen = self.rcent[ww, :].T.copy()
            self.extrapord = np.zeros(self.lcen.shape[1], dtype=np.bool)


    def _pca_order_slit_edges(self):
        """
        Run the order slit edges PCA
          Recommended for ARMED

        Wrapper to trace_slits.pca_order_slit_edges()

        Returns
        -------
        self.lcen  : ndarray (internal)
        self.rcen  : ndarray (internal)
        self.extrapord  : ndarray (internal)

        """
        plxbin = self.pixlocn[:, :, 0].copy()
        self.lcen, self.rcen, self.extrapord \
                = trace_slits.pca_order_slit_edges(self.binarr, self.edgearr, self.lcent,
                                                    self.rcent, self.gord, self.lcoeff,
                                                    self.rcoeff, plxbin, self.slitcen,
                                                    self.pixlocn,
                                                    function=self.par['function'],
                                                    polyorder=self.par['polyorder'],
                                                    diffpolyorder=self.par['diffpolyorder'],
                                                    ofit=self.par['pcapar'],
                                                    extrapolate=self.par['pcaextrap'])
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _pca_pixel_slit_edges(self):
        """
        Run the pixel slit edges PCA
          Recommended for ARMLSD

        Wrapper to trace_slits.pca_pixel_slit_edges()

        Returns
        -------
        self.lcen  : ndarray (internal)
        self.rcen  : ndarray (internal)
        self.extrapord  : ndarray (internal)

        """
        plxbin = self.pixlocn[:, :, 0].copy()
        self.lcen, self.rcen, self.extrapord \
                = trace_slits.pca_pixel_slit_edges(self.binarr, self.edgearr, self.lcoeff,
                                                    self.rcoeff, self.ldiffarr, self.rdiffarr,
                                                    self.lnmbrarr, self.rnmbrarr, self.lwghtarr,
                                                    self.rwghtarr, self.lcent, self.rcent, plxbin,
                                                    function=self.par['function'],
                                                    polyorder=self.par['polyorder'],
                                                    ofit=self.par['pcapar'])
        # Step
        self.steps.append(inspect.stack()[0][3])

    def remove_slit(self, rm_slits, TOL = 3.):
        """
        Remove a user-specified slit

        Wrapper to trace_slits.remove_slit()

        Parameters
        ----------
        rm_slits
          List of slits to remove
            [[left0, right0], [left1, right1]]
          Specified at ycen = nrows//2

        Optional Parameters
        -------------------
        TOL =  tolerance in pixels for grabbing the slit to remove

        Returns
        -------
        self.edgearr  : ndarray (internal)
        self.tc_dict  : dict (internal)
        self.lcen  : ndarray (internal)
        self.rcen  : ndarray (internal)

        """
        self.edgearr, self.lcen, self.rcen, self.tc_dict = trace_slits.remove_slit(
            self.edgearr, self.lcen, self.rcen, self.tc_dict, rm_slits, TOL=TOL)
        # Step
        self.steps.append(inspect.stack()[0][3])

    def reset_edgearr_ednum(self):
        """
        Reset the edgearr numbering using self.ednum
          This is needed when one begins from a full run of TraceSlits

        Returns
        -------
        self.edgearr  : ndarray (internal)

        """
        # Were we really final?
        if np.max(self.edgearr) < self.ednum:
            neg = np.where(self.edgearr < 0)
            self.edgearr[neg] -= (self.ednum - 1)
            pos = np.where(self.edgearr > 0)
            self.edgearr[pos] += (self.ednum - 1)

    def _set_lrminx(self):
        """
        Set lmin, lmax, etc.

        Returns
        -------
        self.lmin : int (intenal)
        self.lmax: int (intenal)
        self.rmin : int (intenal)
        self.rmax: int (intenal)

        """
        ww = np.where(self.edgearr < 0)
        self.lmin, self.lmax = -np.max(self.edgearr[ww]), -np.min(self.edgearr[ww])  # min/max are switched because of the negative signs
        ww = np.where(self.edgearr > 0)
        self.rmin, self.rmax = np.min(self.edgearr[ww]), np.max(self.edgearr[ww])  # min/max are switched because of the negative signs

    def _synchronize(self):
        """
        Perform final synchronization

        Wrapper to trace_slits.synchronize

        Returns
        -------
        Tons and tons..

        """
        plxbin = self.pixlocn[:, :, 0].copy()
        msgs.info("Synchronizing left and right slit traces")
        self.lcent, self.rcent, self.gord, \
            self.lcoeff, self.ldiffarr, self.lnmbrarr, self.lwghtarr, \
            self.rcoeff, self.rdiffarr, self.rnmbrarr, self.rwghtarr \
                = trace_slits.synchronize_edges(self.binarr, self.edgearr, plxbin, self.lmin,
                                                 self.lmax, self.lcoeff, self.rmin, self.rcoeff,
                                                 self.lnmbrarr, self.ldiffarr, self.lwghtarr,
                                                 self.rnmbrarr, self.rdiffarr, self.rwghtarr,
                                                 function=self.par['function'],
                                                 polyorder=self.par['polyorder'],
                                                 extrapolate=self.par['pcaextrap'])
        self.slitcen = 0.5*(self.lcent+self.rcent).T
        # Step
        self.steps.append(inspect.stack()[0][3])

    def _trim_slits(self, trim_short_slits=True, plate_scale = None):
        """
        Trim slits
          Mainly those that fell off the detector
          Or have width less than fracignore

        Parameters
        ----------
        usefracpix : bool, optional
          Trime based on fracignore

        Returns
        -------
        self.lcen  : ndarray (internal)
        self.rcen  : ndarray (internal)

        """
        nslit = self.lcen.shape[1]
        mask = np.zeros(nslit)
        #fracpix = int(self.par['fracignore']*self.mstrace.shape[1])
        for o in range(nslit):
            if np.min(self.lcen[:, o]) > self.mstrace.shape[1]:
                mask[o] = 1
                msgs.info("Slit {0:d} is off the detector - ignoring this slit".format(o+1))
            elif np.max(self.rcen[:, o]) < 0:
                mask[o] = 1
                msgs.info("Slit {0:d} is off the detector - ignoring this slit".format(o + 1))
            if trim_short_slits:
                this_width = np.median(self.rcen[:,o]-self.lcen[:,o])*plate_scale
                if this_width < self.par['min_slit_width']:
                    mask[o] = 1
                    msgs.info("Slit {0:d}".format(o + 1) +
                              " has width = {:}".format(this_width) + " < less than min_slit_width = {:} arcseconds".format(self.par['min_slit_width']) +
                              " - ignoring this slit")
        # Trim
        wok = np.where(mask == 0)[0]
        self.lcen = self.lcen[:, wok]
        self.rcen = self.rcen[:, wok]
        # Step
        self.steps.append(inspect.stack()[0][3])


    def show(self, attr='edges', pstep=50):
        """
        Display an image or spectrum in TraceSlits

        Parameters
        ----------
        attr : str, optional
          'edges' -- Show the mstrace image and the edges
          'edgearr' -- Show the edgearr image
          'siglev' -- Show the Sobolev image
        display : str (optional)
          'ginga' -- Display to an RC Ginga
        """
        if attr == 'edges':
            viewer, ch = ginga.show_image(self.mstrace, chname='edges')
            if self.lcen is not None:
                ginga.show_slits(viewer, ch, self.lcen, self.rcen, slit_ids = np.arange(self.lcen.shape[1]) + 1, pstep=pstep)
        elif attr == 'edgearr':
            # TODO -- Figure out how to set the cut levels
            debugger.show_image(self.edgearr, chname='edgearr')
        elif attr == 'siglev':
            # TODO -- Figure out how to set the cut levels
            debugger.show_image(self.siglev, chname='siglev')

    def save_master(self, root=None, gzip=True):
        """
        Write the main pieces of TraceSlits to the hard drive as a MasterFrame
          FITS -- mstrace and other images
          JSON -- steps, settings, ts_dict

        Parameters
        ----------
        root : str (Optional)
          Path+root name for the output files
        gzip : bool (optional)
          gzip the FITS file (note astropy's method for this is *way* too slow)
        """
        if root is None:
            root = self.ms_name
        # Images
        outfile = root+'.fits'
        hdu = fits.PrimaryHDU(self.mstrace.astype(np.float32))
        hdu.name = 'MSTRACE'
        hdu.header['FRAMETYP'] = 'trace'
        hdulist = [hdu]
        if self.edgearr is not None:
            hdue = fits.ImageHDU(self.edgearr)
            hdue.name = 'EDGEARR'
            hdulist.append(hdue)
        if self.siglev is not None:
            hdus = fits.ImageHDU(self.siglev.astype(np.float32))
            hdus.name = 'SIGLEV'
            hdulist.append(hdus)
        # PIXLOCN -- may be Deprecated
        hdup = fits.ImageHDU(self.pixlocn.astype(np.float32))
        hdup.name = 'PIXLOCN'
        hdulist.append(hdup)
        if self.input_binbpx:  # User inputted
            hdub = fits.ImageHDU(self.binbpx.astype(np.int))
            hdub.name = 'BINBPX'
            hdulist.append(hdub)
        if self.lcen is not None:
            hdulf = fits.ImageHDU(self.lcen)
            hdulf.name = 'LCEN'
            hdulist.append(hdulf)
            hdurt = fits.ImageHDU(self.rcen)
            hdurt.name = 'RCEN'
            hdulist.append(hdurt)
        if self.lcen_tweak is not None:
            hdulf = fits.ImageHDU(self.lcen_tweak)
            hdulf.name = 'LCEN_TWEAK'
            hdulist.append(hdulf)
            hdurt = fits.ImageHDU(self.rcen_tweak)
            hdurt.name = 'RCEN_TWEAK'
            hdulist.append(hdurt)

        # Write
        hdul = fits.HDUList(hdulist)
        hdul.writeto(outfile, overwrite=True)
        msgs.info("Wrote TraceSlit arrays to {:s}".format(outfile))
        if gzip:
            msgs.info("gzip compressing {:s}".format(outfile))
            command = ['gzip', '-f', outfile]
            Popen(command)

        # dict of steps, settings and more
        out_dict = {}
        out_dict['settings'] = parset_to_dict(self.par)
        if self.tc_dict is not None:
            out_dict['tc_dict'] = self.tc_dict
        out_dict['steps'] = self.steps
        # Clean+Write
        outfile = root+'.json'
        clean_dict = ltu.jsonify(out_dict)
        ltu.savejson(outfile, clean_dict, overwrite=True, easy_to_read=True)
        msgs.info("Writing TraceSlit dict to {:s}".format(outfile))

    def load_master(self):
        """
        Over-load the load function

        Returns
        -------

        """
        # Load (externally)
        fits_dict, ts_dict = load_traceslit_files(self.ms_name)
        # Fail?
        if fits_dict is None:
            return False
        # Load up self
        self.binbpx = fits_dict['BINBPX'].astype(float)  # Special
        for key in ['MSTRACE', 'PIXLOCN', 'LCEN', 'RCEN', 'LCEN_TWEAK', 'RCEN_TWEAK', 'EDGEARR', 'SIGLEV']:
            if key in fits_dict.keys():
                setattr(self, key.lower(), fits_dict[key])
        # Remake the binarr
        self.binarr = self.make_binarr()
        # dict
        self.tc_dict = ts_dict['tc_dict']
        # Load the pixel objects?
        self._make_pixel_arrays()
        # Fill
        self._fill_tslits_dict()
        # Success
        return True

    def master_old(self):
        """ Mainly for PYPIT running

        Parameters
        ----------

        Returns
        -------
        loaded : bool

        """
        # Load master frame?
        loaded = False
        if self._masters_load_chk():
            loaded = self.load_master()
        # Return
        return loaded

    def run(self, arms=True, ignore_orders=False, add_user_slits=None, plate_scale = None):
        """ Main driver for tracing slits.

          Code flow
           1.  Determine approximate slit edges (left, right)
             1b.    Trim down to one pixel per edge per row [seems wasteful, but ok]
           2.  Give edges ID numbers + stitch together partial edges (match_edges)
             2b.   first maxgap option -- NOT recommended
           3.  Assign slits (left, right) ::  Deep algorithm
           4.  For ARMLSD
              -- Trace crude the edges
              -- Do a multi-slit sync to pair up left/right edges
           5.  Remove short slits -- Not recommended for ARMLSD
           6.  Fit left/right slits
           7.  Synchronize
           8.  Extrapolate into blank regions (PCA)
           9.  Perform pixel-level calculations

        Parameters
        ----------
        arms : bool (optional)
          Running longslit or multi-slit?
        ignore_orders : bool (optional)
          Perform ignore_orders algorithm (recommended only for echelle data)
        add_user_slits : list of lists
          List of 2 element lists, each an [xleft, xright] pair specifying a slit edge
          These are specified at mstrace.shape[0]//2

        Returns
        -------
        tslits_dict : dict or None (if no slits)
          'lcen'
          'rcen'
          'pixcen'
          'pixwid'
          'lordpix'
          'rordpix'
          'extrapord'
          'slitpix'
        """
        # Specify a single slit?
        if len(self.par['single']) > 0:  # Single slit
            self._edgearr_single_slit()
            self.user_set = True
        else:  # Generate the edgearr from the input trace image
            self._edgearr_from_binarr()
            self.user_set = False

        # Assign a number to each edge 'grouping'
        self._match_edges()

        # Add in a single left/right edge?
        any_slits = self._add_left_right()
        if not any_slits:
            return None

        # If slits are set as "close" by the user, take the absolute value
        # of the detections and ignore the left/right edge detections
        #  Use of maxgap is NOT RECOMMENDED
        if self.par['maxgap'] is not None:
            self._maxgap_prep()

        # Assign edges
        self._assign_edges()

        # Handle close edges (as desired by the user)
        #  JXP does not recommend using this method for multislit
        if self.par['maxgap'] is not None:
            self._maxgap_close()

        # Final left/right edgearr fussing (as needed)
        if not self.user_set:
            self._final_left_right()

        #   Developed for ARMLSD not ARMED
        if arms:
            # Trace crude me
            #   -- Mainly to deal with duplicates and improve the traces
            self._mslit_tcrude()
            # Synchronize and add in edges
            self._mslit_sync()

        # Add user input slits
        if add_user_slits is not None:
            self.add_user_slits(add_user_slits)

        # Ignore orders/slits on the edge of the detector when they run off
        #    Recommended for Echelle only
        if ignore_orders:
            self._ignore_orders()

        # Fit edges
        self._set_lrminx()
        self._fit_edges('left')
        self._fit_edges('right')

        # Are we done, e.g. longslit?
        #   Check if no further work is needed (i.e. there only exists one order)
        if self._chk_for_longslit():
            self.extrapord = np.zeros(1, dtype=np.bool)
        else:  # No, not done yet
            # Synchronize
            #   For multi-silt, mslit_sync will have done most of the work already..
            self._synchronize()

            # PCA?
            #  Whether or not a PCA is performed, lcen and rcen are generated for the first time
            self._pca()

            # Remove any slits that are completely off the detector
            #   Also remove short slits here for multi-slit and long-slit (aligntment stars)
            self._trim_slits(trim_short_slits=arms, plate_scale = plate_scale)

        # Generate pixel arrays
        self._make_pixel_arrays()

        # fill dict for PYPIT
        self.tslits_dict = self._fill_tslits_dict()

        # Return it
        return self.tslits_dict

    def _qa(self, use_slitid=True):
        """
        QA
          Wrapper to trace_slits.slit_trace_qa()

        Returns
        -------

        """
        trace_slits.slit_trace_qa(self.mstrace, self.lcen,
                                   self.rcen, self.extrapord, self.setup,
                                   desc="Trace of the slit edges D{:02d}".format(self.det),
                                   use_slitid=use_slitid, out_dir=self.redux_path)


    def __repr__(self):
        # Generate sets string
        txt = '<{:s}: '.format(self.__class__.__name__)
        if len(self.steps) > 0:
            txt+= ' steps: ['
            for step in self.steps:
                txt += '{:s}, '.format(step)
            txt = txt[:-2]+']'  # Trim the trailing comma
        txt += '>'
        return txt



def load_traceslit_files(root):
    """
    Load up the TraceSlit objects from the FITS and JSON file

    Pushed out of the class so we can both load and instantiate
    from the output files

    Parameters
    ----------
    root : str

    Returns
    -------
    fits_dict : dict
      Contains all the images from the FITS file
    ts_dict : dict
      JSON read
    """
    fits_dict = {}
    # Open FITS
    fits_file = root+'.fits.gz'
    if not os.path.isfile(fits_file):
        msgs.warn("No TraceSlits FITS file found.  Returning None, None")
        return None, None

    msgs.info("Loading a pre-existing master calibration frame of type: trace from filename: {:}".format(fits_file))
    hdul = fits.open(fits_file)
    names = [ihdul.name for ihdul in hdul]
    if 'SLITPIXELS' in names:
        msgs.error("This is an out-of-date MasterTrace flat.  You will need to make a new one")

    # Load me
    for key in ['MSTRACE', 'PIXLOCN', 'BINBPX', 'LCEN', 'RCEN', 'LCEN_TWEAK', 'RCEN_TWEAK', 'EDGEARR', 'SIGLEV']:
        if key in names:
            fits_dict[key] = hdul[names.index(key)].data

    # JSON
    json_file = root+'.json'
    ts_dict = ltu.loadjson(json_file)

    # Return
    return fits_dict, ts_dict


