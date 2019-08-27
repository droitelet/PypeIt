""" Object to hold + process a single image"""

from pypeit import msgs
from pypeit import ginga

import numpy as np

from astropy.io import fits

from pypeit.core import save
from pypeit.images import maskimage
from pypeit.io import initialize_header

from IPython import embed


class PypeItImage(maskimage.ImageMask):
    """
    Class to hold a single image from a single detector in PypeIt
    Oriented in its spec,spat format

    The intent is to keep this object as light-weight as possible.
    Therefore methods to generate, save, load, etc. are all outside the Class

    Args:
        spectrograph (:class:`pypeit.spectrographs.spectrograph.Spectrograph`):
            Spectrograph used to take the data.
        det (:obj:`int`, optional):
            The 1-indexed detector number to process.

    Attributes:
        image (np.ndarray):
        head0 (astropy.io.fits.Header):

    """

    def __init__(self, image, ivar=None, rn2img=None, bpm=None, state=None,
                 binning=None, crmask=None, mask=None):

        maskimage.ImageMask.__init__(self, bpm)

        # Required parameters
        self.image = image

        # Optional Attributes
        self.ivar = ivar
        self.rn2img = rn2img
        self.state = state
        self.binning = binning
        self.head0 = None

        # Mask attributes
        self.crmask = crmask
        self.mask = mask

        # Data model
        self.allowed_attributes = ('image', 'ivar', 'rn2img') + self.mask_attributes

    def show(self):
        """
        Simple show method
        """
        if self.image is None:
            msgs.warn("No image to show!")
            return
        ginga.show_image(self.image, chname='image')

    def __repr__(self):
        repr = '<{:s}: '.format(self.__class__.__name__)
        # Image
        rdict = {}
        for attr in ['image', 'ivar', 'rn2img', 'crmask', 'mask']:
            if getattr(self, attr) is not None:
                rdict[attr] = True
            else:
                rdict[attr] = False
        repr += ' images={}'.format(rdict)
        repr = repr + '>'
        return repr


def save_images(pypeitImage, outfile, hdr=None, iext=None):
    """
    Write the image(s) to a multi-extension FITS file

    Cannot be named save as that is a core module

    Extensions will be:
       PRIMARY
       IMAGE
       IVAR (optional)
       MASK (optional)

    Args:
        outfile:
        iext (str, optional):
            Name for the first extension
            Defaults to IMAGE

    Returns:

    """
    if hdr is None:
        hdr = initialize_header()

    # Save whatever is available
    data = [pypeitImage.image]
    if iext is None:
        ext = ['IMAGE']
    else:
        ext = [iext]

    # Load up the rest
    for item in ['ivar', 'mask']:
        if getattr(pypeitImage, item) is not None:
            data.append(getattr(pypeitImage, item))
            ext.append(item.upper())

    # TODO -- Default to float32 for float images?
    # Write the fits file
    save.write_fits(hdr, data, outfile, extnames=ext)


def load_images(file):
    """
    Load a PypeItImage from disk (FITS file)

    Args:
        file (str):

    Returns:
        PypeItImage: Loaded up PypeItImage with the primary Header attached

    """
    # Open
    hdul = fits.open(file)
    # Header
    head0 = hdul[0].header

    # Instantiate
    pypeitImage = PypeItImage(hdul[1].data)
    pypeitImage.head0 = head0
    if hdul[1].name != 'IMAGE':
        msgs.warn("Badly formated PypeItImage.  I hope this is an old calibration frame for compatibility")

    for kk in range(2,len(hdul)):
        # Check
        if hdul[kk].name.lower() not in pypeitImage.allowed_attributes:
            msgs.warn('Badly formatted PypeItImage')
            msgs.error('Extenstion {} is not an allowed attribute of {}'.format(hdul[kk].name, pypeitImage.allowed_attributes))
        # Continue
        setattr(pypeitImage, hdul[kk].name.lower(), hdul[kk].data)

    # Return
    return pypeitImage
