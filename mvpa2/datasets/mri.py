# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the PyMVPA package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Support for magnetic resonance imaging (MRI) data IO.

This module offers functions to import into PyMVPA MRI data from files
in any format supported by NiBabel_ (e.g. NIfTI, MINC, Analyze), and
export PyMVPA datasets back into data formats supported by NiBabel_.

.. _NiBabel: http://nipy.sourceforge.net/nibabel
"""

__docformat__ = 'restructuredtext'

from mvpa2.base import externals
externals.exists('nibabel', raise_=True)

import numpy as np
from mvpa2.base.dataset import _expand_attribute

if __debug__:
    from mvpa2.base import debug

from mvpa2.datasets.base import Dataset
from mvpa2.mappers.flatten import FlattenMapper
from mvpa2.base import warning


def _hdr2dict(hdr):
    """Helper to convert a NiBabel image header to a dict of arrays"""
    kv = dict(hdr)

    # checking/mitigating the problem on Windows with nibabel 2.0.0
    # see https://github.com/PyMVPA/PyMVPA/issues/302#issuecomment-99882488
    scl_slope = str(kv.get('scl_slope', ''))
    scl_inter = str(kv.get('scl_inter', ''))
    if scl_slope == scl_inter == 'nan':
        warning("Detected incorrect (nan) scl_ fields. Resetting to "
                "scl_slope=1.0 and scl_inter=0.0")
        kv['scl_slope'] = 1.0
        kv['scl_inter'] = 0.0
    kv['hdrtype'] = hdr.__class__.__name__
    return kv


def _dict2hdr(kv):
    """Counterpart of ``_hdr2dict()``"""
    import nibabel
    hdr = getattr(nibabel, kv['hdrtype'])()
    for k in kv:
        if k == 'hdrtype':
            continue
        hdr[k] = kv[k]
    return hdr


def _img2data(src):
    # break early of nothing has been given
    # XXX feels a little strange to handle this so deep inside, but well...
    if src is None:
        return None

    # let's try whether we can get it done with nibabel
    import nibabel
    if isinstance(src, basestring):
        # filename
        img = nibabel.load(src)
    else:
        # assume this is an image already
        img = src
    if isinstance(img, nibabel.spatialimages.SpatialImage):

        data, header = img.get_data(), img.header

        if len(img.shape) == 5 and img.shape[3] == 1:
            # hack to allow loading NIFTI files generated by AFNI
            # these have time in the fifth dimension while the fourth
            # dimension is singleton
            warning('dataset with 5th dimension found but 4th is empty (AFNI '
                    ' NIFTI conversion syndrome) - squeezing data to 4D')

            s = img.shape
            newshape = (s[0], s[1], s[2], s[4])

            header.set_data_shape(newshape)
            data = np.reshape(data, newshape)

        # nibabel image, dissect and return pieces
        return _get_txyz_shaped(data), header, img
    else:
        # no clue what it is
        return None


def map2nifti(dataset, data=None, imghdr=None, imgtype=None):
    """Maps data(sets) into the original dataspace and wraps it into an Image.

    Parameters
    ----------
    dataset : Dataset
      The mapper of this dataset is used to perform the reverse-mapping.
    data : ndarray or Dataset, optional
      The data to be wrapped into NiftiImage. If None (default), it
      would wrap samples of the provided dataset. If it is a Dataset
      instance -- takes its samples for mapping.
    imghdr : None or dict, optional
      Image header data. If None, the header is taken from `dataset.a.imghdr`.
    imgtype : None or class, optional
      Image class to be used for the instance. If None, the type is taken
      from `dataset.a.imgtype`.

    Returns
    -------
    Image
      Instance of a class derived from :class:`nibabel.spatialimages.SpatialImage`,
      such as Nifti1Image
    """
    import nibabel
    if data is None:
        data = dataset.samples
    elif isinstance(data, Dataset):
        # ease users life
        data = data.samples

    # call the appropriate function to map single samples or multiples
    if len(data.shape) > 1:
        dsarray = dataset.a.mapper.reverse(data)
    else:
        dsarray = dataset.a.mapper.reverse1(data)

    if imghdr is None:
        if 'imghdr' in dataset.a:
            imghdr = dataset.a.imghdr
        elif __debug__:
            debug('DS_NIFTI', 'No image header found. Using defaults.')
    if imghdr is not None:
        if 'hdrtype' in imghdr:
            imghdr = _dict2hdr(imghdr)
        else:
            # fall back on previous logic and expect a header instance
            # we can fail in glorious ways further down
            pass

    if imgtype is None:
        if 'imgtype' in dataset.a:
            try:
                imgtype = getattr(nibabel, dataset.a.imgtype)
            except TypeError:
                # fall back on previous logic and expect an actual type
                imgtype = dataset.a.imgtype
        else:
            imgtype = nibabel.Nifti1Image
            if __debug__:
                debug('DS_NIFTI',
                      'No image type found in %s. Using default Nifti1Image.'
                      % (dataset.a))

    # set meaningful range
    try:
        if imghdr is not None:
            if 'cal_max' in imghdr:
                imghdr['cal_max'] = dsarray.max()
                imghdr['cal_min'] = dsarray.min()
    except:
        # probably not a NIfTI header
        pass

    # Augment header if data dsarray dtype could not be represented
    # with imghdr.get_data_dtype()

    if issubclass(imgtype, nibabel.spatialimages.SpatialImage) \
            and (imghdr is None or hasattr(imghdr, 'get_data_dtype')):
        # we can handle the desired image type and hdr with nibabel
        # use of `None` for the affine should cause to pull it from
        # the header
        return imgtype(_get_xyzt_shaped(dsarray), None, imghdr)
    else:
        raise ValueError(
            "Got imgtype=%s and imghdr=%s -- cannot generate an Image"
            % (imgtype, imghdr))
    return RuntimeError("Should have never got here -- check your Python")


def fmri_dataset(samples, targets=None, chunks=None, mask=None,
                 sprefix='voxel', tprefix='time', add_fa=None,):
    """Create a dataset from an fMRI timeseries image.

    The timeseries image serves as the samples data, with each volume becoming
    a sample. All 3D volume samples are flattened into one-dimensional feature
    vectors, optionally being masked (i.e. subset of voxels corresponding to
    non-zero elements in a mask image).

    In addition to (optional) samples attributes for targets and chunks the
    returned dataset contains a number of additional attributes:

    Samples attributes (per each volume):

      * volume index (time_indices)
      * volume acquisition time (time_coord)

    Feature attributes (per each voxel):

      * voxel indices (voxel_indices), sometimes referred to as ijk

    Dataset attributes:

      * dump of the image (e.g. NIfTI) header data (imghdr)
      * class of the image (e.g. Nifti1Image) (imgtype)
      * volume extent (voxel_dim)
      * voxel extent (voxel_eldim)

    The default attribute name is listed in parenthesis, but may be altered by
    the corresponding prefix arguments. The validity of the attribute values
    relies on correct settings in the NIfTI image header.

    Parameters
    ----------
    samples : str or NiftiImage or list
      fMRI timeseries, specified either as a filename (single file 4D image),
      an image instance (4D image), or a list of filenames or image instances
      (each list item corresponding to a 3D volume).
    targets : scalar or sequence
      Label attribute for each volume in the timeseries, or a scalar value that
      is assigned to all samples.
    chunks : scalar or sequence
      Chunk attribute for each volume in the timeseries, or a scalar value that
      is assigned to all samples.
    mask : str or NiftiImage
      Filename or image instance of a 3D volume mask. Voxels corresponding to
      non-zero elements in the mask will be selected. The mask has to be in the
      same space (orientation and dimensions) as the timeseries image
    sprefix : str or None
      Prefix for attribute names describing spatial properties of the
      timeseries. If None, no such attributes are stored in the dataset.
    tprefix : str or None
      Prefix for attribute names describing temporal properties of the
      timeseries. If None, no such attributes are stored in the dataset.
    add_fa : dict or None
      Optional dictionary with additional volumetric data that shall be stored
      as feature attributes in the dataset. The dictionary key serves as the
      feature attribute name. Each value might be of any type supported by the
      'mask' argument of this function.

    Returns
    -------
    Dataset
    """
    # load the samples
    imgdata, imghdr, img = _load_anyimg(samples, ensure=True, enforce_dim=4)

    # figure out what the mask is, but only handle known cases, the rest
    # goes directly into the mapper which maybe knows more
    maskimg = _load_anyimg(mask)
    if maskimg is None:
        pass
    else:
        # take just data and ignore the header
        mask = maskimg[0]

    # compile the samples attributes
    sa = {}
    if targets is not None:
        sa['targets'] = _expand_attribute(targets, imgdata.shape[0], 'targets')
    if chunks is not None:
        sa['chunks'] = _expand_attribute(chunks, imgdata.shape[0], 'chunks')

    # create a dataset
    ds = Dataset(imgdata, sa=sa)
    if sprefix is None:
        space = None
    else:
        space = sprefix + '_indices'
    ds = ds.get_mapped(FlattenMapper(shape=imgdata.shape[1:], space=space))

    # now apply the mask if any
    if mask is not None:
        flatmask = ds.a.mapper.forward1(mask)
        # direct slicing is possible, and it is potentially more efficient,
        # so let's use it
        #mapper = StaticFeatureSelection(flatmask)
        #ds = ds.get_mapped(StaticFeatureSelection(flatmask))
        ds = ds[:, flatmask != 0]

    # load and store additional feature attributes
    if add_fa is not None:
        for fattr in add_fa:
            value = _load_anyimg(add_fa[fattr], ensure=True)[0]
            ds.fa[fattr] = ds.a.mapper.forward1(value)

    # store interesting NIfTI props in the dataset in a more portable way
    ds.a['imgaffine'] = img.affine
    ds.a['imgtype'] = img.__class__.__name__
    # stick the header instance in as is, and ...
    ds.a['imghdr'] = imghdr
    # ... let strip_nibabel() be the central place to take care of any header
    # conversion into non-NiBabel dtypes
    strip_nibabel(ds)

    # If there is a space assigned , store the extent of that space
    if sprefix is not None:
        ds.a[sprefix + '_dim'] = imgdata.shape[1:]
        # 'voxdim' is (x,y,z) while 'samples' are (t,z,y,x)
        ds.a[sprefix + '_eldim'] = _get_voxdim(imghdr)
        # TODO extend with the unit
    if tprefix is not None:
        ds.sa[tprefix + '_indices'] = np.arange(len(ds), dtype='int')
        ds.sa[tprefix + '_coords'] = \
            np.arange(len(ds), dtype='float') * _get_dt(imghdr)
        # TODO extend with the unit

    return ds


def _get_voxdim(hdr):
    """Get the size of a voxel from some image header format."""
    return hdr.get_zooms()[:-1]


def _get_dt(hdr):
    """Get the TR of a fMRI timeseries from some image header format."""
    return hdr.get_zooms()[-1]


def _get_txyz_shaped(arr):
    # we get the data as x,y,z[,t] but we want to have the time axis first
    # if any
    if len(arr.shape) == 4:
        arr = np.rollaxis(arr, -1)
    return arr


def _get_xyzt_shaped(arr):
    # we get the data as [t,]x,y,z but we want to have the time axis last
    # if any
    if len(arr.shape) == 4:
        arr = np.rollaxis(arr, 0, 4)
    return arr


def _load_anyimg(src, ensure=False, enforce_dim=None):
    """Load/access NIfTI data from files or instances.

    Parameters
    ----------
    src : str or NiftiImage
      Filename of a NIfTI image or a `NiftiImage` instance.
    ensure : bool, optional
      If True, throw ValueError exception if cannot be loaded.
    enforce_dim : int or None
      If not None, it is the dimensionality of the data to be enforced,
      commonly 4D for the data, and 3D for the mask in case of fMRI.

    Returns
    -------
    tuple or None
      If the source is not supported None is returned.  Otherwise a
      tuple of (imgdata, imghdr, img)

    Raises
    ------
    ValueError
      If there is a problem with data (variable dimensionality) or
      failed to load data and ensure=True.
    """
    imgdata = imghdr = None

    # figure out whether we have a list of things to load and handle that
    # first
    if (isinstance(src, list) or isinstance(src, tuple)) \
            and len(src) > 0:
        # load from a list of given entries
        srcs = [_load_anyimg(s, ensure=ensure, enforce_dim=enforce_dim)
                for s in src]
        if __debug__:
            # lets check if they all have the same dimensionality
            # besides the leading one
            shapes = [s[0].shape[1:] for s in srcs]
            if not np.all([s == shapes[0] for s in shapes]):
                raise ValueError(
                    "Input volumes vary in their shapes: %s" % (shapes,))
        # Combine them all into a single beast
        # will be t,x,y,z
        imgdata = np.vstack([s[0] for s in srcs])
        imghdr, img = srcs[0][1:3]
    else:
        # try opening the beast; this might yield none in case of an unsupported
        # argument and is handled accordingly below
        data = _img2data(src)
        if data is not None:
            imgdata, imghdr, img = data

    if imgdata is not None and enforce_dim is not None:
        shape, new_shape = imgdata.shape, None
        lshape = len(shape)

        # check if we need to tune up shape
        if lshape < enforce_dim:
            # if we are missing required dimension(s)
            new_shape = (1,) * (enforce_dim - lshape) + shape
        elif lshape > enforce_dim:
            # if there are bogus dimensions at the beginning
            bogus_dims = lshape - enforce_dim
            if shape[:bogus_dims] != (1,) * bogus_dims:
                raise ValueError("Cannot enforce %dD on data with shape %s"
                                 % (enforce_dim, shape))
            new_shape = shape[bogus_dims:]

        # tune up shape if needed
        if new_shape is not None:
            if __debug__:
                debug('DS_NIFTI', 'Enforcing shape %s for %s data from %s' %
                      (new_shape, shape, src))
            imgdata.shape = new_shape

    if imgdata is None:
        return None
    else:
        return imgdata, imghdr, img


def strip_nibabel(ds):
    """Strip NiBabel objects from a dataset (in-place modification).

    Prior PyMVPA version 2.4, datasets created from MRI data used to contain
    NiBabel objects, such as image header instances. As a consequence,
    re-loading such datasets from a serialized form (e.g. form HDF5 files) can
    suffer from NiBabel API changes, and sometimes prevent loading completely.

    This function converts these NiBabel internals into a simpler form that
    helps to process such datasets with a much wider range of NiBabel
    versions, and removes the need to have NiBabel installed for simply
    loading such a dataset.

    Run this function on a dataset to modify it in-place and make it more
    robust for storage in HDF5 format or other forms of serialization.

    It is safe to run this function on already converted datasets. The
    resulting datasets require PyMVPA v2.4 or later for exporting into the
    NIfTI format, but are otherwise compatible with any 2.x version as well.

    Parameters
    ----------
    ds : Dataset
      To be converted dataset

    Returns
    -------
    None
      Modification is done in-place.
    """
    # only str class name is stored
    if 'imgtype' in ds.a and isinstance(ds.a.imgtype, type):
        ds.a['imgtype'] = ds.a.imgtype.__name__
    if 'imghdr' not in ds.a:
        return
    if hasattr(ds.a.imghdr, 'get_best_affine'):
        # new dataset store the affine directly
        # it may already have one, but the header might have a better idea
        ds.a['imgaffine'] = ds.a.imghdr.get_best_affine()
    if isinstance(ds.a.imghdr, dict):
        # nothing to do
        # this test may be incomplete but it is cheap. All NiBabel header
        # instances should not pass it
        return
    # we still have a header that is something complicated
    try:
        # make an attempt to store more of the image header in a simple
        # dict(array), but no moaning if that fails -- some image types
        # don't support that, e.g. MINC
        ds.a['imghdr'] = _hdr2dict(ds.a.imghdr)
    except:
        if __debug__:
            debug('DS_NIFTI',
                  'Failed to store header info as attribute (src: %s)'
                  % (ds.a.imghdr.__class__,))
        # when conversion fails, we need to kill the remains
        del ds.a['imghdr']
    return ds
