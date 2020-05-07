from __future__ import print_function 
from pixell import enmap, utils, resample
import numpy as np
from pixell.fft import fft,ifft
from scipy.interpolate import interp1d
import yaml,six
from orphics import io,cosmology,stats
import math
from scipy.interpolate import RectBivariateSpline,interp2d,interp1d
import warnings
import healpy as hp

def galactic_mask(shape,wcs,nside,theta1,theta2):
    npix = hp.nside2npix(nside)
    orig = np.ones(npix)
    orig[hp.query_strip(nside,theta1,theta2)] = 0
    return reproject.ivar_hp_to_cyl(orig, shape, wcs, rot=True,do_mask=False,extensive=False)

def north_galactic_mask(shape,wcs,nside):
    return galactic_mask(shape,wcs,nside,0,np.deg2rad(90))

def south_galactic_mask(shape,wcs,nside):
    return galactic_mask(shape,wcs,nside,np.deg2rad(90),np.deg2rad(180))


def rms_from_ivar(ivar,parea=None,cylindrical=True):
    """
    Return rms noise for each pixel in a map in physical units
    (uK-arcmin) given a map of the inverse variance per pixel.
    Optionally, provide a map of the pixel area.
    """
    if parea is None:
        shape,wcs = ivar.shape, ivar.wcs
        parea = psizemap(shape,wcs) if cylindrical else enmap.pixsizemap(shape,wcs)
    return np.sqrt((1./ivar)*parea)*180*60./np.pi

    

def psizemap(shape,wcs):
    """
    Return map of pixel areas in radians for a cylindrical map.
    Contrast with enmap.pixsizemap which is not specific to cylindrical
    maps but is not accurate near the poles at the time of this writing.
    """
    dra, ddec = wcs.wcs.cdelt*utils.degree
    dec = enmap.posmap([shape[-2],1],wcs)[0,:,0]
    area = np.abs(dra*(np.sin(np.minimum(np.pi/2.,dec+ddec/2))-np.sin(np.maximum(-np.pi/2.,dec-ddec/2))))
    Nx = shape[-1]
    return enmap.ndmap(area[...,None].repeat(Nx,axis=-1),wcs)

def ivar(shape,wcs,noise_muK_arcmin,ipsizemap=None):
    if ipsizemap is None: ipsizemap = psizemap(shape,wcs)
    pmap = ipsizemap*((180.*60./np.pi)**2.)
    return pmap/noise_muK_arcmin**2.

def white_noise(shape,wcs,noise_muK_arcmin,seed=None,ipsizemap=None):
    """
    Generate a non-band-limited white noise map.
    """
    div = ivar(shape,wcs,noise_muK_arcmin,ipsizemap=ipsizemap)
    if seed is not None: np.random.seed(seed)
    return np.random.standard_normal(shape) / np.sqrt(div)

    
def get_ecc(img):
    """Returns eccentricity from central moments of image
    """
    from skimage import measure
    
    M = measure.moments_central(np.asarray(img),order=2)
    Cov = np.array([[M[2,0],M[1,1]],
                    [M[1,1],M[0,2]]])/M[0,0]

    mu20 = M[2,0]/M[0,0]
    mu11 = M[1,1]/M[0,0]
    mu02 = M[0,2]/M[0,0]

    l1 = (mu20+mu02)/2. + np.sqrt(4.*mu11**2.+(mu20-mu02)**2.)/2.
    l2 = (mu20+mu02)/2. - np.sqrt(4.*mu11**2.+(mu20-mu02)**2.)/2.


    e = np.sqrt(1.-l2/l1)
    return e

def filter_alms(alms,lmin,lmax):
    import healpy as hp
    ells = np.arange(0,lmax+20,1)
    fs = np.ones(ells.shape)
    fs[ells<lmin] = 0.
    fs[ells>lmax] = 0.
    return hp.almxfl(alms,fs)


def rotate_pol_power(shape,wcs,cov,iau=False,inverse=False):
    """Rotate a 2D power spectrum from TQU to TEB (inverse=False) or
    back (inverse=True). cov is a (3,3,Ny,Nx) 2D power spectrum.
    WARNING: This function is duplicated in orphics.pixcov to make 
    that module independent. Ideally, it should be implemented in
    enlib.enmap.
    """
    assert np.all(np.isfinite(cov))
    rot = np.zeros((3,3,cov.shape[-2],cov.shape[-1]))
    rot[0,0,:,:] = 1
    prot = enmap.queb_rotmat(enmap.lmap(shape,wcs), inverse=inverse, iau=iau)
    rot[1:,1:,:,:] = prot
    Rt = np.transpose(rot, (1,0,2,3))
    tmp = np.einsum("ab...,bc...->ac...",rot,cov)
    rp2d = np.einsum("ab...,bc...->ac...",tmp,Rt)    
    return rp2d


def binary_mask(mask,threshold=0.5):
    m = np.abs(mask)
    m[m<=threshold] = 0
    m[m>threshold] = 1
    return m
        

def area_from_mask(mask):
    m = binary_mask(mask)
    frac = m.sum()*1./np.prod(m.shape[-2:])
    return frac*mask.area()*(180./np.pi)**2., frac

def get_central(img,fracy,fracx=None):
    if fracy is None and fracx is None: return img
    fracx = fracy if fracx is None else fracx
    Ny,Nx = img.shape[-2:]
    cropy = int(fracy*Ny)
    cropx = int(fracx*Nx)
    if cropy%2==0 and Ny%2==1:
        cropy -= 1
    else:
        if cropy%2==1 and Ny%2==0: cropy -= 1
    if cropx%2==0 and Nx%2==1:
        cropx -= 1
    else:
        if cropx%2==1 and Nx%2==0: cropx -= 1
    return crop_center(img,cropy,cropx)

def crop_center(img,cropy,cropx=None,sel=False):
    cropx = cropy if cropx is None else cropx
    y,x = img.shape[-2:]
    startx = x//2-(cropx//2)
    starty = y//2-(cropy//2)
    selection = np.s_[...,starty:starty+cropy,startx:startx+cropx]
    if sel:
        ret = selection
    else:
        ret = img[selection]
    return ret

def binned_power(imap,bin_edges=None,binner=None,fc=None,modlmap=None,imap2=None,mask=1):
    """Get the binned power spectrum of a map in one line of code.
    (At the cost of flexibility and reusability of expensive parts)"""
    
    from orphics import stats
    shape,wcs = imap.shape,imap.wcs
    modlmap = enmap.modlmap(shape,wcs) if modlmap is None else modlmap
    fc = FourierCalc(shape,wcs) if fc is None else fc
    binner = stats.bin2D(modlmap,bin_edges) if binner is None else binner
    p2d,_,_ = fc.power2d(imap*mask,imap2*mask if imap2 is not None else None)
    cents,p1d = binner.bin(p2d)
    return cents,p1d/np.mean(mask**2.)

def interp(x,y,bounds_error=False,fill_value=0.,**kwargs):
    return interp1d(x,y,bounds_error=bounds_error,fill_value=fill_value,**kwargs)

def flat_sim(deg,px,lmax=6000,lensed=True,pol=False):
    """
    Get some commonly used objects for flat-sky sims.
    Not very flexible but is a one-line replacement for
    a large fraction of use cases.
    """
    from orphics import cosmology
    shape,wcs = rect_geometry(width_deg=deg,px_res_arcmin=px,pol=pol)
    modlmap = enmap.modlmap(shape,wcs)
    cc = cosmology.Cosmology(lmax=lmax,pickling=True,dimensionless=False)
    Lmax = modlmap.max()
    ells = np.arange(0,Lmax,1)
    ps = cosmology.power_from_theory(ells,cc.theory,lensed=lensed,pol=pol)
    mgen = MapGen(shape,wcs,ps)
    return shape,wcs,modlmap,cc,mgen


def resample_fft(imap,res):
    """
    Wrapper around enlib.resample.resample_fft.
    Accepts a target map resolution instead of target shape.
    Returns an enmap instead of an array.
    imap must be periodic/windowed
    """
    shape,wcs = imap.shape,imap.wcs
    inres = resolution(shape,wcs)
    scale = inres/res
    oshape,owcs = enmap.scale_geometry(shape, wcs, scale)
    return enmap.enmap(resample.resample_fft(imap,oshape[-2:]),owcs)


def resampled_geometry(shape,wcs,res):
    inres = resolution(shape,wcs)
    scale = inres/res
    oshape,owcs = enmap.scale_geometry(shape, wcs, scale)
    return oshape,owcs


def split_sky(dec_width,num_decs,ra_width,dec_start=0.,ra_start=0.,ra_extent=90.):

    ny = num_decs
    wy = dec_width
    xw = ra_width
    boxes = []
    for yindex in range(ny):
        y0 = dec_start+yindex*wy
        y1 = dec_start+(yindex+1)*wy
        ymean = (y0+y1)/2.
        cosfact = np.cos(ymean*np.pi/180.)
        xfw = ra_extent*cosfact
        nx = int(xfw/xw)

        for xindex in range(nx):
            x0 = ra_start+xindex*xw/cosfact
            x1 = ra_start+(xindex+1)*xw/cosfact
            box = np.array([[y0,x0],[y1,x1]])
            boxes.append(box.copy())
    return boxes


def slice_from_box(shape, wcs, box, inclusive=False):
    """slice_from_box(shape, wcs, box, inclusive=False)
    Extract the part of the map inside the given box as a selection
    without returning the data. Does not work for boxes
    that straddle boundaries of maps. Use enmap.submap instead.
    Parameters
    ----------
    box : array_like
	    The [[fromy,fromx],[toy,tox]] bounding box to select.
	    The resulting map will have a bounding box as close
	    as possible to this, but will differ slightly due to
	    the finite pixel size.
    inclusive : boolean
		Whether to include pixels that are only partially
		inside the bounding box. Default: False."""
    ibox = enmap.subinds(shape, wcs, box, inclusive)
    print(shape,ibox)
    islice = utils.sbox2slice(ibox.T)
    return islice
    
def cutup(shape,numy,numx,pad=0):
    Ny,Nx = shape
    pixs_y = np.linspace(0,shape[-2],num=numy+1,endpoint=True)	
    pixs_x = np.linspace(0,shape[-1],num=numx+1,endpoint=True)
    num_boxes = numy*numx
    boxes = np.zeros((num_boxes,2,2))
    boxes[:,0,0] = np.tile(pixs_y[:-1],numx) - pad
    boxes[:,0,0][boxes[:,0,0]<0] = 0
    boxes[:,1,0] = np.tile(pixs_y[1:],numx) + pad
    boxes[:,1,0][boxes[:,1,0]>(Ny-1)] = Ny-1
    boxes[:,0,1] = np.repeat(pixs_x[:-1],numy) - pad
    boxes[:,0,1][boxes[:,0,1]<0] = 0
    boxes[:,1,1] = np.repeat(pixs_x[1:],numy) + pad
    boxes[:,1,1][boxes[:,1,1]>(Nx-1)] = Nx-1
    boxes = boxes.astype(np.int)

    return boxes


def bounds_from_list(blist):
    """Given blist = [dec0,ra0,dec1,ra1] in degrees
    return ndarray([[dec0,ra0],[dec1,ra1]]) in radians
    """
    return np.array(blist).reshape((2,2))*np.pi/180.
        

def rect_geometry(width_arcmin=None,width_deg=None,px_res_arcmin=0.5,proj="car",pol=False,height_deg=None,height_arcmin=None,xoffset_degree=0.,yoffset_degree=0.,extra=False,**kwargs):
    """
    Get shape and wcs for a rectangular patch of specified size and coordinate center
    """

    if width_deg is not None:
        width_arcmin = 60.*width_deg
    if height_deg is not None:
        height_arcmin = 60.*height_deg
    
    hwidth = width_arcmin/2.
    if height_arcmin is None:
        vwidth = hwidth
    else:
        vwidth = height_arcmin/2.
    arcmin =  utils.arcmin
    degree =  utils.degree
    pos = [[-vwidth*arcmin+yoffset_degree*degree,-hwidth*arcmin+xoffset_degree*degree],[vwidth*arcmin+yoffset_degree*degree,hwidth*arcmin+xoffset_degree*degree]]
    shape, wcs = enmap.geometry(pos=pos, res=px_res_arcmin*arcmin, proj=proj,**kwargs)
    if pol: shape = (3,)+shape
    if extra:
        modlmap = enmap.modlmap(shape,wcs)
        lmax = modlmap.max()
        ells = np.arange(0,lmax,1.)
        return shape,wcs,modlmap,ells
    else:
        return shape, wcs


def downsample_power(shape,wcs,cov,ndown=16,order=0,exp=None,fftshift=True,fft=False,logfunc=lambda x: x,ilogfunc=lambda x: x,fft_up=False):
    """
    Smooth a power spectrum by averaging. This can be used to, for example:
    1. calculate a PS for use in a noise model
    2. calculate an ILC covariance empirically in Fourier-Cartesian domains

    shape -- tuple specifying shape of 
    """

    if ndown<1: return cov
    ndown = np.array(ndown).ravel()
    if ndown.size==1:
        Ny,Nx = shape[-2:]
        nmax = max(Ny,Nx)
        nmin = min(Ny,Nx)
        ndown1 = ndown[0]
        ndown2 = int(ndown*nmax*1./nmin)
        ndown = np.array((ndown2,ndown1)) if Ny>Nx else np.array((ndown1,ndown2))
    else:
        assert ndown.size==2
        ndown = np.array((ndown[0],ndown[1]))
    print("Downsampling power spectrum by factor ", ndown)
        
        
    cov = logfunc(cov)
    afftshift = np.fft.fftshift if fftshift else lambda x: x
    aifftshift = np.fft.ifftshift if fftshift else lambda x: x
    if fft:
        dshape = np.array(cov.shape)
        dshape[-2] /= ndown[0]
        dshape[-1] /= ndown[1]
        cov_low = resample.resample_fft(afftshift(cov), dshape.astype(np.int))
    else:
        cov_low = enmap.downgrade(afftshift(cov), ndown)
    if not(fft_up):
        pix_high = enmap.pixmap(shape[-2:],wcs)
        pix_low = pix_high/ndown.reshape((2,1,1))
        
    if exp is not None:
        covexp = enmap.enmap(enmap.multi_pow(cov_low,exp),wcs)
    else:
        covexp = enmap.enmap(cov_low,wcs)


    if fft_up:
        retcov = resample.resample_fft(covexp, shape)
    else:
        retcov = covexp.at(pix_low, order=order, mask_nan=False, unit="pix")
        
    return ilogfunc(aifftshift(retcov))
    

class MapGen(object):
        """
        Once you know the shape and wcs of an ndmap and the input power spectra, you can 
        pre-calculate some things to speed up random map generation.
        """
        
        def __init__(self,shape,wcs,cov=None,covsqrt=None,pixel_units=False,smooth="auto",ndown=None,order=1):
                self.shape = shape
                self.wcs = wcs
                assert cov.ndim>=3 , "Power spectra have to be of shape (ncomp,ncomp,lmax) or (ncomp,ncomp,Ny,Nx)."
                if covsqrt is not None:
                    self.covsqrt = covsqrt
                else:
                    if cov.ndim==4:
                            if not(pixel_units): cov = cov * np.prod(shape[-2:])/enmap.area(shape,wcs )
                            if ndown:
                                self.covsqrt = downsample_power(shape,wcs,cov,ndown,order,exp=0.5)
                            else:
                                self.covsqrt = enmap.multi_pow(cov, 0.5)
                    else:
                            self.covsqrt = enmap.spec2flat(shape, wcs, cov, 0.5, mode="constant",smooth=smooth)


        def get_map(self,seed=None,scalar=False,iau=True,real=False):
                if seed is not None: np.random.seed(seed)
                rand = enmap.fft(enmap.rand_gauss(self.shape, self.wcs)) if real else enmap.rand_gauss_harm(self.shape, self.wcs)
                data = enmap.map_mul(self.covsqrt, rand)
                kmap = enmap.ndmap(data, self.wcs)
                if scalar:
                        return enmap.ifft(kmap).real
                else:
                        return enmap.harm2map(kmap,iau=iau)

        
        
def spec1d_to_2d(shape,wcs,ps):
    return enmap.spec2flat(shape,wcs,ps)/(np.prod(shape[-2:])/enmap.area(shape,wcs ))
    
class FourierCalc(object):
    """
    Once you know the shape and wcs of an ndmap, you can pre-calculate some things
    to speed up fourier transforms and power spectra.
    """

    def __init__(self,shape,wcs,iau=True):
        """Initialize with a geometry shape and wcs."""
        
        self.shape = shape
        self.wcs = wcs
        self.normfact = enmap.area(self.shape,self.wcs )/ np.prod(self.shape[-2:])**2.         
        if len(shape) > 2 and shape[-3] > 1:
            self.rot = enmap.queb_rotmat(enmap.lmap(shape,wcs),iau=iau)

    def iqu2teb(self,emap, nthread=0, normalize=True, rot=True):
        """Performs the 2d FFT of the enmap pixels, returning a complex enmap.
        Similar to harm2map, but uses a pre-calculated self.rot matrix.
        """
        emap = enmap.samewcs(enmap.fft(emap,nthread=nthread,normalize=normalize), emap)
        if emap.ndim > 2 and emap.shape[-3] > 1 and rot:
            emap[...,-2:,:,:] = enmap.map_mul(self.rot, emap[...,-2:,:,:])

        return emap


    def f2power(self,kmap1,kmap2,pixel_units=False):
        """Similar to power2d, but assumes both maps are already FFTed """
        norm = 1. if pixel_units else self.normfact
        res = np.real(np.conjugate(kmap1)*kmap2)*norm
        return res

    def f1power(self,map1,kmap2,pixel_units=False,nthread=0):
        """Similar to power2d, but assumes map2 is already FFTed """
        kmap1 = self.iqu2teb(map1,nthread,normalize=False)
        norm = 1. if pixel_units else self.normfact
        return np.real(np.conjugate(kmap1)*kmap2)*norm,kmap1

    def ifft(self,kmap):
        return enmap.enmap(ifft(kmap,axes=[-2,-1],normalize=True),self.wcs)
    
    def fft(self,emap):
        return enmap.samewcs(enmap.fft(emap,normalize=False), emap)
        

    def power2d(self,emap=None, emap2=None,nthread=0,pixel_units=False,skip_cross=False,rot=True, kmap=None, kmap2=None, dtype=None):
        """
        Calculate the power spectrum of emap crossed with emap2 (=emap if None)
        Returns in radians^2 by default unles pixel_units specified
        """
        wcs = emap.wcs if emap is not None else kmap.wcs
        if kmap is not None:
            lteb1 = kmap
            ndim = kmap.ndim
            if ndim>2 : ncomp = kmap.shape[-3]
        else:
            lteb1 = self.iqu2teb(emap,nthread,normalize=False,rot=rot)
            ndim = emap.ndim
            if ndim>2 : ncomp = emap.shape[-3]

        if kmap2 is not None:
            lteb2 = kmap2
        else:
            lteb2 = self.iqu2teb(emap2,nthread,normalize=False,rot=rot) if emap2 is not None else lteb1
                
        assert lteb1.shape==lteb2.shape
                
        if ndim > 2 and ncomp > 1:
            retpow = np.zeros((ncomp,ncomp,lteb1.shape[-2],lteb1.shape[-1]),dtype=dtype)
            for i in range(ncomp):
                retpow[i,i] = self.f2power(lteb1[i],lteb2[i],pixel_units)
            if not(skip_cross):
                for i in range(ncomp):
                    for j in range(i+1,ncomp):
                        retpow[i,j] = self.f2power(lteb1[i],lteb2[j],pixel_units)
                        retpow[j,i] = retpow[i,j]
            return retpow,lteb1,lteb2
        else:
            if lteb1.ndim>2:
                lteb1 = lteb1[0]
            if lteb2.ndim>2:
                lteb2 = lteb2[0]
            p2d = self.f2power(lteb1,lteb2,pixel_units)
            return enmap.enmap(p2d,wcs),enmap.enmap(lteb1,wcs),enmap.enmap(lteb2,wcs)



class MapRotator(object):
    def __init__(self,shape_source,wcs_source,shape_target,wcs_target):
        self.pix_target = get_rotated_pixels(shape_source,wcs_source,shape_target,wcs_target)
    def rotate(self,imap,**kwargs):
        return rotate_map(imap,pix_target=self.pix_target,**kwargs)

class MapRotatorEquator(MapRotator):
    def __init__(self,shape_source,wcs_source,patch_width,patch_height,width_multiplier=1.,
                 height_multiplier=1.5,pix_target_override_arcmin=None,proj="car",verbose=False,
                 downsample=True,downsample_pix_arcmin=None):
        
        self.source_pix =  np.min(enmap.extent(shape_source,wcs_source)/shape_source[-2:])*60.*180./np.pi
        if pix_target_override_arcmin is None:
            input_dec = enmap.posmap(shape_source,wcs_source)[0]
            max_dec = np.max(np.abs(input_dec))
            del input_dec
            recommended_pix = self.source_pix*np.cos(max_dec)

            if verbose:
                print("INFO: Maximum declination in southern patch : ",max_dec*180./np.pi, " deg.")
                print("INFO: Recommended pixel size for northern patch : ",recommended_pix, " arcmin")

        else:
            recommended_pix = pix_target_override_arcmin
            
        shape_target,wcs_target = rect_geometry(width_arcmin=width_multiplier*patch_width*60.,
                                                      height_arcmin=height_multiplier*patch_height*60.,
                                                      px_res_arcmin=recommended_pix,yoffset_degree=0.,proj=proj)


        self.target_pix = recommended_pix
        self.wcs_target = wcs_target
        if verbose:
            print("INFO: Source pixel : ",self.source_pix, " arcmin")
        
        if downsample:
            dpix = downsample_pix_arcmin if downsample_pix_arcmin is not None else self.source_pix

            self.shape_final,self.wcs_final = rect_geometry(width_arcmin=width_multiplier*patch_width*60.,
                                              height_arcmin=height_multiplier*patch_height*60.,
                                              px_res_arcmin=dpix,yoffset_degree=0.,proj=proj)
        else:
            self.shape_final = shape_target
            self.wcs_final = wcs_target
        self.downsample = downsample
            
        MapRotator.__init__(self,shape_source,wcs_source,shape_target,wcs_target)

    def rotate(self,imap,**kwargs):
        rotated = MapRotator.rotate(self,imap,**kwargs)

        if self.downsample:
            from pixell import resample
            return enmap.ndmap(resample.resample_fft(rotated,self.shape_final),self.wcs_final)
        else:
            return rotated
    
def get_rotated_pixels(shape_source,wcs_source,shape_target,wcs_target,inverse=False,pos_target=None,center_target=None,center_source=None):
    """ Given a source geometry (shape_source,wcs_source)
    return the pixel positions in the target geometry (shape_target,wcs_target)
    if the source geometry were rotated such that its center lies on the center
    of the target geometry.

    WARNING: Only currently tested for a rotation along declination from one CAR
    geometry to another CAR geometry.
    """

    from pixell import coordinates
    
    # what are the center coordinates of each geometries
    if center_source is None: center_source = enmap.pix2sky(shape_source,wcs_source,(shape_source[0]/2.,shape_source[1]/2.))
    if center_target is None: center_target = enmap.pix2sky(shape_target,wcs_target,(shape_target[0]/2.,shape_target[1]/2.))
    decs,ras = center_source
    dect,rat = center_target

    # what are the angle coordinates of each pixel in the target geometry
    if pos_target is None: pos_target = enmap.posmap(shape_target,wcs_target)
    lra = pos_target[1,:,:].ravel()
    ldec = pos_target[0,:,:].ravel()
    del pos_target

    # recenter the angle coordinates of the target from the target center to the source center
    if inverse:
        newcoord = coordinates.decenter((lra,ldec),(rat,dect,ras,decs))
    else:
        newcoord = coordinates.recenter((lra,ldec),(rat,dect,ras,decs))
    del lra
    del ldec

    # reshape these new coordinates into enmap-friendly form
    new_pos = np.empty((2,shape_target[0],shape_target[1]))
    new_pos[0,:,:] = newcoord[1,:].reshape(shape_target)
    new_pos[1,:,:] = newcoord[0,:].reshape(shape_target)
    del newcoord

    # translate these new coordinates to pixel positions in the target geometry based on the source's wcs
    pix_new = enmap.sky2pix(shape_source,wcs_source,new_pos)
    return pix_new

def rotate_map(imap,shape_target=None,wcs_target=None,pix_target=None,**kwargs):
    if pix_target is None:
        pix_target = get_rotated_pixels(shape_source,wcs_source,shape_target,wcs_target)
    else:
        assert (shape_target is None) and (wcs_target is None), "Both pix_target and shape_target,wcs_target must not be specified."

    rotmap = enmap.at(imap,pix_target,unit="pix",**kwargs)
    return rotmap
    
                    
### REAL AND FOURIER SPACE ATTRIBUTES

def angmap(shape,wcs,iau=False):
    sgn = -1 if iau else 1
    lmap = enmap.lmap(shape,wcs)
    return sgn*np.arctan2(-lmap[1], lmap[0])

def get_ft_attributes(shape,wcs):
    shape = shape[-2:]
    Ny, Nx = shape
    pixScaleY, pixScaleX = enmap.pixshape(shape,wcs)

    
        
    lx =  2*np.pi  * np.fft.fftfreq( Nx, d = pixScaleX )
    ly =  2*np.pi  * np.fft.fftfreq( Ny, d = pixScaleY )
    
    ix = np.mod(np.arange(Nx*Ny),Nx)
    iy = np.arange(Nx*Ny)//Nx
    
    modlmap = enmap.modlmap(shape,wcs)
    
    theta_map = np.zeros([Ny,Nx])
    theta_map[iy[:],ix[:]] = np.arctan2(ly[iy[:]],lx[ix[:]])


    lxMap, lyMap = np.meshgrid(lx, ly)  # is this the right order?

    return lxMap,lyMap,modlmap,theta_map,lx,ly


def get_real_attributes(shape,wcs):
    Ny, Nx = shape[-2:]
    pixScaleY, pixScaleX = enmap.pixshape(shape,wcs)
    
    return get_real_attributes_generic(Ny,Nx,pixScaleY,pixScaleX)

def get_real_attributes_generic(Ny,Nx,pixScaleY,pixScaleX):
    
    xx =  (np.arange(Nx)-Nx/2.+0.5)*pixScaleX
    yy =  (np.arange(Ny)-Ny/2.+0.5)*pixScaleY
    
    ix = np.mod(np.arange(Nx*Ny),Nx)
    iy = np.arange(Nx*Ny)/Nx
    
    modRMap = np.zeros([Ny,Nx])
    modRMap[iy,ix] = np.sqrt(xx[ix]**2 + yy[iy]**2)
    

    xMap, yMap = np.meshgrid(xx, yy)  # is this the right order?

    return xMap,yMap,modRMap,xx,yy


## MAXLIKE

def diagonal_cov(power2d):
    ny,nx = power2d.shape[-2:]
    assert power2d.ndim==2 or power2d.ndim==4
    if power2d.ndim == 2: power2d = power2d[None,None]
    ncomp = len(power2d)
    
    Cflat = np.zeros((ncomp,ncomp,nx*ny,nx*ny))

    # ncomp=3 at most so let's just for loop it without trying to be clever
    # Sigurd suggests
    # np.einsum("...ii->...i", Cflat)[:] = power2d.reshape(Cflat.shape[:-1])
    # but warns it might break with older numpy versions
    for i in range(ncomp):
        for j in range(ncomp):
            np.fill_diagonal(Cflat[i,j],power2d[i,j].reshape(-1))
    return Cflat.reshape((ncomp,ncomp,ny,nx,ny,nx))


def ncov(shape,wcs,noise_uk_arcmin):
    noise_uK_rad = noise_uk_arcmin*np.pi/180./60.
    normfact = np.sqrt(np.prod(enmap.pixsize(shape[-2:],wcs)))
    noise_uK_pixel = noise_uK_rad/normfact
    return np.diag([(noise_uK_pixel)**2.]*np.prod(shape[-2:]))


def pixcov(shape,wcs,fourier_cov):
    #fourier_cov = fourier_cov.astype(np.float32, copy=False)
    fourier_cov = fourier_cov.astype(np.complex64, copy=False)
    bny,bnx = shape[-2:]
    #from numpy.fft import fft2 as hfft,ifft2 as hifft # TODO: update to fast fft
    from pixell.fft import fft as hfft,ifft as hifft # This doesn't work ValueError:
    # Invalid scheme: The output array and input array dtypes do not correspond to a valid fftw scheme.


    #pcov = hfft((hifft(fourier_cov,axes=(-4,-3))),axes=(-2,-1)).real
    pcov = hfft((hifft(fourier_cov,axes=(-4,-3),normalize=True)),axes=(-2,-1)).real # gotta normalize if using enlib.fft
    return pcov*bnx*bny/enmap.area(shape,wcs)

def get_lnlike(covinv,instamp):
    Npix = instamp.size
    assert covinv.size==Npix**2
    vec = instamp.reshape((Npix,1))
    ans = np.dot(np.dot(vec.T,covinv),vec)
    assert ans.size==1
    return ans[0,0]



def pixcov_sim(shape,wcs,ps,Nsims,seed=None,mean_sub=True,pad=0):
    if pad>0:
        retmap = enmap.pad(enmap.zeros(shape,wcs), pad, return_slice=False, wrap=False)
        oshape,owcs = retmap.shape,retmap.wcs
    else:
        oshape,owcs = shape,wcs
        
    
    mg = MapGen(oshape,owcs,ps)
    np.random.seed(seed)
    umaps = []
    for i in range(Nsims):
        cmb = mg.get_map()
        if mean_sub: cmb -= cmb.mean()

        if pad>0:
            ocmb = enmap.extract(cmb, shape, wcs)
        else:
            ocmb = cmb
        umaps.append(ocmb.ravel())
        
    pixcov = np.cov(np.array(umaps).T)
    return pixcov


## MAP OPERATIONS



def butterworth(ells,ell0,n):
    return 1./(1.+(ells*1./ell0)**(2.*n))


def get_taper(shape,wcs,taper_percent = 12.0,pad_percent = 3.0,weight=None):
    Ny,Nx = shape[-2:]
    if weight is None: weight = np.ones(shape[-2:])
    taper = cosine_window(Ny,Nx,lenApodY=int(taper_percent*min(Ny,Nx)/100.),lenApodX=int(taper_percent*min(Ny,Nx)/100.),padY=int(pad_percent*min(Ny,Nx)/100.),padX=int(pad_percent*min(Ny,Nx)/100.))*weight
    w2 = np.mean(taper**2.)
    return enmap.enmap(taper,wcs),w2

def get_taper_deg(shape,wcs,taper_width_degrees = 1.0,pad_width_degrees = 0.,weight=None,only_y = False):
    Ny,Nx = shape[-2:]
    if weight is None: weight = np.ones(shape[-2:])
    res = resolution(shape,wcs)
    pix_apod = int(taper_width_degrees*np.pi/180./res)
    pix_pad = int(pad_width_degrees*np.pi/180./res)
    taper = enmap.enmap(cosine_window(Ny,Nx,lenApodY=pix_apod,lenApodX=pix_apod if not(only_y) else 0,padY=pix_pad,padX=pix_pad if not(only_y) else 0)*weight,wcs)
    w2 = np.mean(taper**2.)
    return taper,w2


def cosine_window(Ny,Nx,lenApodY=30,lenApodX=30,padY=0,padX=0):
    # Based on a routine by Thibaut Louis
    win=np.ones((Ny,Nx))
    i = np.arange(Nx) 
    j = np.arange(Ny)
    ii,jj = np.meshgrid(i,j)
    # ii is array of x indices
    # jj is array of y indices
    # numpy indexes (j,i)
    # xdirection
    if lenApodX>0:
        r=ii.astype(float)-padX
        sel = np.where(ii<=(lenApodX+padX))
        win[sel] = 1./2*(1-np.cos(-np.pi*r[sel]/lenApodX))
        sel = np.where(ii>=((Nx-1)-lenApodX-padX))
        r=((Nx-1)-ii-padX).astype(float)
        win[sel] = 1./2*(1-np.cos(-np.pi*r[sel]/lenApodX))
    # ydirection
    if lenApodY>0:
        r=jj.astype(float)-padY
        sel = np.where(jj<=(lenApodY+padY))
        win[sel] *= 1./2*(1-np.cos(-np.pi*r[sel]/lenApodY))
        sel = np.where(jj>=((Ny-1)-lenApodY-padY))
        r=((Ny-1)-jj-padY).astype(float)
        win[sel] *= 1./2*(1-np.cos(-np.pi*r[sel]/lenApodY))
    win[0:padY,:]=0
    win[:,0:padX]=0
    win[Ny-padY:,:]=0
    win[:,Nx-padX:]=0
    return win

def filter_map(imap,kfilter):
    return enmap.enmap(np.real(ifft(fft(imap,axes=[-2,-1])*kfilter,axes=[-2,-1],normalize=True)) ,imap.wcs)

def gauss_beam(ell,fwhm):
    tht_fwhm = np.deg2rad(fwhm / 60.)
    return np.exp(-(tht_fwhm**2.)*(ell**2.) / (16.*np.log(2.)))

def sigma_from_fwhm(fwhm):
    return fwhm/2./np.sqrt(2.*np.log(2.))

def gauss_beam_real(rs,fwhm):
    """rs in radians ; fwhm in arcmin"""
    tht_fwhm = np.deg2rad(fwhm / 60.)
    sigma = sigma_from_fwhm(tht_fwhm)
    return np.exp(-(rs**2.) / 2./sigma**2.)


def mask_kspace(shape,wcs, lxcut = None, lycut = None, lmin = None, lmax = None):
    output = np.ones(shape[-2:], dtype = int)
    if (lmin is not None) or (lmax is not None): modlmap = enmap.modlmap(shape, wcs)
    if (lxcut is not None) or (lycut is not None): ly, lx = enmap.laxes(shape, wcs, oversample=1)
    if lmin is not None:
        output[np.where(modlmap <= lmin)] = 0
    if lmax is not None:
        output[np.where(modlmap >= lmax)] = 0
    if lxcut is not None:
        output[:,np.where(np.abs(lx) < lxcut)] = 0
    if lycut is not None:
        output[np.where(np.abs(ly) < lycut),:] = 0
    return output

## ILC (MAP-LEVEL AND SPEC-LEVEL)

def silc(kmaps,cinv,response=None):
    """Make a simple internal linear combination (ILC) of given fourier space maps at different frequencies
    and an inverse covariance matrix for its variance.

    From Eq 4 of arXiv:1006.5599
    
    Accepts
    -------

    kmaps -- (nfreq,Ny,Nx) array of beam-deconvolved fourier transforms at each frequency
    cinv -- (nfreq,nfreq,Ny,Nx) array of the inverted covariance matrix
    response -- (nfreq,) array of f_nu response factors. Defaults to unity for CMB estimate.

    Returns
    -------

    Fourier transform of ILC estimate, (Ny,Nx) array 
    """
    response = ilc_def_response(response,cinv)
    # Get response^T Cinv kmaps
    weighted = ilc_map_term(kmaps,cinv,response)
    return weighted * silc_noise(cinv,response)

def cilc(kmaps,cinv,response_a,response_b):
    """Constrained ILC -- Make a constrained internal linear combination (ILC) of given fourier space maps at different frequencies
    and an inverse covariance matrix for its variance. The component of interest is specified through its f_nu response vector
    response_a. The component to explicitly project out is specified through response_b.

    Derived from Eq 18 of arXiv:1006.5599

    Accepts
    -------

    kmaps -- (nfreq,Ny,Nx) array of beam-deconvolved fourier transforms at each frequency
    cinv -- (nfreq,nfreq,Ny,Nx) array of the inverted covariance matrix
    response_a -- (nfreq,) array of f_nu response factors for component of interest.
    response_b -- (nfreq,) array of f_nu response factors for component to project out.

    Returns
    -------

    Fourier transform of ILC estimate, (Ny,Nx) array 
    """
    
    brb = ilc_comb_a_b(response_b,response_b,cinv)
    arb = ilc_comb_a_b(response_a,response_b,cinv)
    arM = ilc_map_term(kmaps,cinv,response_a)
    brM = ilc_map_term(kmaps,cinv,response_b)
    ara = ilc_comb_a_b(response_a,response_a,cinv)

    numer = brb * arM - arb*brM
    norm = (ara*brb-arb**2.)
    return np.nan_to_num(numer/norm)

def ilc_def_response(response,cinv):
    """Default CMB response -- vector of ones"""
    if response is None:
        # assume CMB
        nfreq = cinv.shape[0]
        response = np.ones((nfreq,))
    return response

def ilc_index(ndim):
    """Returns einsum indexing given ndim of cinv.
    If covmat of 1d powers, return single index, else
    return 2 indices for 2D kspace matrix."""
    if ndim==3:
        return "p"
    elif ndim==4:
        return "ij"
    else:
        raise ValueError

def silc_noise(cinv,response=None):
    """ Derived from Eq 4 of arXiv:1006.5599"""
    response = ilc_def_response(response,cinv)
    return np.nan_to_num(1./ilc_comb_a_b(response,response,cinv))

def cilc_noise(cinv,response_a,response_b):
    """ Derived from Eq 18 of arXiv:1006.5599 """
    
    brb = ilc_comb_a_b(response_b,response_b,cinv)
    ara = ilc_comb_a_b(response_a,response_a,cinv)
    arb = ilc_comb_a_b(response_a,response_b,cinv)
    bra = ilc_comb_a_b(response_b,response_a,cinv)

    numer = (brb)**2. * ara + (arb)**2.*brb - brb*arb*arb - arb*brb*bra
    denom = (ara*brb-arb**2.)**2.
    return np.nan_to_num(numer/denom)


def ilc_map_term(kmaps,cinv,response):
    """response^T . Cinv . kmaps """
    return np.einsum('k,k...->...',response,np.einsum('kl...,l...->k...',cinv,kmaps))
    
def ilc_comb_a_b(response_a,response_b,cinv):
    """Return a^T cinv b"""
    pind = ilc_index(cinv.ndim) # either "p" or "ij" depending on whether we are dealing with 1d or 2d power
    return np.nan_to_num(np.einsum('l,l...->...',response_a,np.einsum('k,kl...->l...',response_b,cinv)))


def ilc_empirical_cov(kmaps,bin_edges=None,ndown=16,order=1,fftshift=True,method="isotropic"):
    assert method in ['isotropic','downsample']
    
    assert kmaps.ndim==3
    ncomp = kmaps.shape[0]

    if method=='isotropic':
        modlmap = enmap.modlmap(kmaps[0].shape,kmaps.wcs)
        binner = stats.bin2D(modlmap,bin_edges)
        from scipy.interpolate import interp1d

    
    retpow = np.zeros((ncomp,ncomp,kmaps.shape[-2],kmaps.shape[-1]))
    for i in range(ncomp):
        for j in range(i+1,ncomp):
            retpow[i,j] = np.real(kmaps[i]*kmaps[j].conj())
            if method=='isotropic':
                cents,p1d = binner.bin(retpow[i,j])
                retpow[i,j] = interp1d(cents,p1d,fill_value="extrapolate",bounds_error=False)(modlmap)
            retpow[j,i] = retpow[i,j].copy()

    if method=='isotropic':
        return retpow
    elif method=='downsample':
        return downsample_power(retpow.shape,kmaps[0].wcs,retpow,ndown=ndown,order=order,exp=None,fftshift=fftshift,fft=False,logfunc=lambda x: x,ilogfunc=lambda x: x,fft_up=False)


def ilc_cov(ells,cmb_ps,kbeams,freqs,noises,components,fnoise=None,plot=False,
            plot_save=None,ellmaxes=None,data=True,fgmax=None,
            narray=None,fdict=None,verbose=True,analysis_beam=1.,lmins=None,lmaxs=None):
    """
    ells -- either 1D or 2D fourier wavenumbers
    cmb_ps -- Theory C_ell_TT in 1D or 2D fourier space
    kbeams -- 1d or 2d beam transforms
    freqs -- array of floats with frequency bandpasses
    noises -- 1d, 2d or float noise powers (in uK^2-radian^2)
    components -- list of strings representing foreground components recognized by fgGenerator
    fnoise -- A szar.foregrounds.fgNoises object (or derivative) containing foreground power definitions

    Returns beam-deconvolved covariance matrix
    """

    nfreqs = len(freqs)
    if cmb_ps.ndim==2:
        cshape = (nfreqs,nfreqs,1,1)
    elif cmb_ps.ndim==1:
        cshape = (nfreqs,nfreqs,1)
    else:
        raise ValueError

    Covmat = np.tile(cmb_ps,cshape)*analysis_beam**2.

    for i,freq1 in enumerate(freqs):
        for j,freq2 in enumerate(freqs):
            if verbose: print("Populating covariance for ",freq1,"x",freq2)
            if narray is not None:
                Covmat[i,j,...] += narray[i,j,...]
            else:
                if i==j:
                    kbeam1 = kbeams[i]
                    noise1 = noises[i]
                    instnoise = np.nan_to_num(noise1*analysis_beam**2./kbeam1**2.) 
                    Covmat[i,j,...] += instnoise

            for component in components:
                if fdict is None:
                    fgnoise = fnoise.get_noise(component,freq1,freq2,ells)
                else:
                    fgnoise = np.nan_to_num(fdict[component](ells,freq1,freq2))
                fgnoise[np.abs(fgnoise)>1e90] = 0
                if (fgmax is not None) and component=='tsz':
                    fgnoise[ells>fgmax] = fgnoise[fgmax]
                fgnoise = fgnoise * analysis_beam**2.
                Covmat[i,j,...] += fgnoise

            if data:
                Covmat[i,j][ells>ellmaxes[i]] = 1e90 # !!!
                Covmat[i,j][ells>ellmaxes[j]] = 1e90 # !!!
            #if i>=j:
            #    io.plot_img(np.fft.fftshift(np.log10(Covmat[i,j,:])),lim=[-10,3])
            if i==j:
                if lmins is not None: Covmat[i,j][ells<lmins[i]] = np.inf
                if lmaxs is not None: Covmat[i,j][ells>lmaxs[i]] = np.inf

                

    return Covmat

def ilc_cinv(ells,cmb_ps,kbeams,freqs,noises,components,fnoise,plot=False,plot_save=None,eigpow=True,ellmaxes=None,data=True,fgmax=None,narray=None):
    """
    ells -- either 1D or 2D fourier wavenumbers
    cmb_ps -- Theory C_ell_TT in 1D or 2D fourier space
    kbeams -- 1d or 2d beam transforms
    freqs -- array of floats with frequency bandpasses
    noises -- 1d, 2d or float noise powers (in uK^2-radian^2)
    components -- list of strings representing foreground components recognized by fgGenerator
    fnoise -- A szar.foregrounds.fgNoises object (or derivative) containing foreground power definitions

    Returns beam-deconvolved inv covariance matrix
    """
    Covmat = np.nan_to_num(ilc_cov(ells,cmb_ps,kbeams,freqs,noises,components,fnoise,plot,plot_save,ellmaxes=ellmaxes,data=data,fgmax=fgmax,narray=narray))
    print("Inverting covariance...")

    if eigpow:
        from pixell import utils
        cinv = utils.eigpow(Covmat, -1.,axes=[0,1])
        return cinv,Covmat
    else:
        cinv = np.linalg.inv(Covmat.T).T
        return cinv


def minimum_ell(shape,wcs):
    """
    Returns the lowest angular wavenumber of an ndmap
    rounded down to the nearest integer.
    """
    modlmap = enmap.modlmap(shape,wcs)
    min_ell = modlmap[modlmap>0].min()
    return int(min_ell)



def resolution(shape,wcs):
    res = np.min(np.abs(enmap.extent(shape,wcs))/shape[-2:])
    return res


def inpaint_cg(imap,rand_map,mask,power2d,eps=1.e-8):

    """
    by Thibaut Louis

    imap  -- masked map
    rand_map  -- random map with same power
    mask -- mask
    power2d -- 2d S+N power : IMPORTANT, this must be non-zero up to pixel scale
    eps

    """

    assert imap.ndim==2
    nyside,nxside = imap.shape
    
    def apply_px_c_inv_px(my_map):
        
        my_map.shape=(nyside,nxside)
        #apply x_proj
        my_new_map=(1-mask)*my_map
        # change to Fourrier representation
        a_l = fft(my_new_map,axes=[-2,-1])
        # apply inverse power spectrum
        a_l=a_l*1/power2d
        # change back to pixel representation
        my_new_map = ifft(a_l,normalize=True,axes=[-2,-1])
        #Remove the imaginary part
        my_new_map=my_new_map.real
        # apply x_proj
        my_new_map=(1-mask)*my_new_map
        #Array to vector
        my_new_map.shape=(nxside*nyside,)
        my_map.shape=(nxside*nyside,)
        return(my_new_map)
    
    def apply_px_c_inv_py(my_map):
        # apply y_proj
        my_map.shape=(nyside,nxside)
        my_new_map=mask*my_map
        # change to Fourrier representation
        a_l = fft(my_new_map,axes=[-2,-1])
        # apply inverse power spectrum
        a_l=a_l*1/power2d
        # change back to pixel representation
        my_new_map = ifft(a_l,normalize=True,axes=[-2,-1])
        #Remove the imaginary part
        my_new_map=my_new_map.real
        # apply x_proj
        my_new_map=(1-mask)*my_new_map
        #Array to vector
        my_new_map.shape=(nxside*nyside,)
        return(my_new_map)
    
    b=-apply_px_c_inv_py(imap-rand_map)
    
    #Number of iterations
    i_max=2000
    
    #initial value of x
    x=b
    i=0
    
    r=b-apply_px_c_inv_px(x)
    d=r
    
    delta_new=np.inner(r,r)
    delta_o=delta_new
    
    delta_array=np.zeros(shape=(i_max))
    
    while i<i_max and delta_new > eps**2*delta_o:
        # print ("")
        # print ("number of iterations:", i)
        # print ("")
        # print ("eps**2*delta_o=",eps**2*delta_o)
        # print ("")
        # print ("delta new=",delta_new)
        
        q=apply_px_c_inv_px(d)
        alpha=delta_new/(np.inner(d,q))
        x=x+alpha*d
        
        if i/50.<np.int(i/50):
            
            r=b-apply_px_c_inv_px(x)
        else:
            r=r-alpha*q
        
        delta_old=delta_new
        delta_new=np.inner(r,r)
        beta=delta_new/delta_old
        d=r+beta*d
        i=i+1
    
    #print "delta_o=", delta_o
    #print "delta_new=", delta_new
    
    x.shape=(nyside,nxside)
    x_old=x
    x=x+rand_map*(1-mask)
    complete=imap*mask
    rebuild_map=complete+x
    print("Num iterations : ",i)
    return rebuild_map


## WORKING WITH DATA

class DataNoise(object):

    def __init__(self,splits,wmaps,taper):

        h0 = wmaps[0]
        ht = np.zeros(h0.shape)
        for h1 in wmaps:
            ht[h1!=0] += (h1[h1!=0]**-2)
        ht[ht!=0] = 1./ht[ht!=0]


class NoiseModel(object):
    # Deprecated?

    def __init__(self,splits=None,wmap=None,mask=None,kmask=None,directory=None,spec_smooth_width=2.,skip_beam=True,skip_mask=True,skip_kmask=True,skip_cross=True,iau=False):
        """
        shape, wcs for geometry
        unmasked splits
        hit counts wmap
        real-space mask that must include a taper. Can be 3-dimensional if Q/U different from I.
        k-space kmask
        """

        if directory is not None:
            self._load(directory,skip_beam,skip_mask,skip_kmask,skip_cross)
        else:
            
            shape = splits[0].shape
            wcs = splits[0].wcs

        
            if wmap is None: wmap = enmap.ones(shape[-2:],wcs)
            if mask is None: mask = np.ones(shape[-2:])
            if kmask is None: kmask = np.ones(shape[-2:])

            wmap = enmap.ndmap(wmap,wcs)

            osplits = [split*mask for split in splits]
            fc = FourierCalc(shape,wcs,iau)
            n2d, p2d = noise_from_splits(osplits,fc)
            del osplits
            del splits
            w2 = np.mean(mask**2.)
            n2d *= (1./w2)
            p2d *= (1./w2)

            n2d = np.fft.ifftshift(enmap.smooth_spectrum(np.fft.fftshift(n2d), kernel="gauss", weight="mode", width=spec_smooth_width))
            self.spec_smooth_width = spec_smooth_width
            ncomp = shape[0] if len(shape)>2 else 1
            
            self.cross2d = p2d
            self.cross2d *= kmask
            n2d *= kmask
            self.noise2d = n2d.reshape((ncomp,ncomp,shape[-2],shape[-1]))
            self.mask = mask
            self.kmask = kmask
            self.wmap = wmap
            self.shape = shape
            self.wcs = wcs
        self.ngen = MapGen(self.shape,self.wcs,self.noise2d)
        # self.noise_modulation = 1./np.sqrt(self.wmap)/np.sqrt(np.mean((1./self.wmap)))
        wt = 1./np.sqrt(self.wmap)
        wtw2 = np.mean(1./wt**2.)
        self.noise_modulation = wt*np.sqrt(wtw2)


    def _load(self,directory,skip_beam=True,skip_mask=True,skip_kmask=True,skip_cross=True):

        self.wmap = enmap.read_hdf(directory+"/wmap.hdf")
        
        self.wcs = self.wmap.wcs
        self.noise2d = np.load(directory+"/noise2d.npy")
        if not(skip_mask): self.mask = enmap.read_hdf(directory+"/mask.hdf")
        if not(skip_cross): self.cross2d = np.load(directory+"/cross2d.npy")
        if not(skip_beam): self.kbeam2d = np.load(directory+"/kbeam2d.npy")
        if not(skip_kmask): self.kmask = np.load(directory+"/kmask.npy")
        loadnum = np.loadtxt(directory+"/spec_smooth_width.txt")
        assert loadnum.size==1
        self.spec_smooth_width = float(loadnum.ravel()[0])

        loadnum = np.loadtxt(directory+"/shape.txt")
        self.shape = tuple([int(x) for x in loadnum.ravel()])

    def save(self,directory):
        io.mkdir(directory)
        
        
        enmap.write_hdf(directory+"/wmap.hdf",self.wmap)
        enmap.write_hdf(directory+"/mask.hdf",self.mask)
        np.save(directory+"/noise2d.npy",self.noise2d)
        np.save(directory+"/cross2d.npy",self.cross2d)
        np.save(directory+"/kmask.npy",self.kmask)
        try: np.save(directory+"/kbeam2d.npy",self.kbeam2d)
        except: pass
        np.savetxt(directory+"/spec_smooth_width.txt",np.array(self.spec_smooth_width).reshape((1,1)))
        np.savetxt(directory+"/shape.txt",np.array(self.shape))


        
        

    def get_noise_sim(self,seed=None,noise_mod=True):
        nval = self.noise_modulation if noise_mod else 1.
        return self.ngen.get_map(seed=seed,scalar=True) * nval
        
    def add_beam_1d(self,ells,beam_1d_transform):
        modlmap = enmap.modlmap(self.shape[-2:],self.wcs)
        self.kbeam2d = interp1d(ells,beam_1d_transform,bounds_error=False,fill_value=0.)(modlmap)
    def add_beam_2d(self,beam_2d_transform):
        assert self.shape[-2:]==beam_2d_transform.shape
        self.kbeam2d = beam_2d_transform


def split_calc(isplits,jsplits,icoadd,jcoadd,fourier_calc=None,alt=True):
    """
    Calculate the best estimate of the signal (from mean of crosses)
    and of the noise (total - mean crosses) power.

    isplits (and jsplits) are (nsplits,Ny,Nx) fourier transforms of
    windowed maps. No window correction is applied to the result.
    No polarization rotation is done either.
    """
    shape,wcs = isplits.shape,isplits.wcs
    assert isplits.ndim==3
    fc = fourier_calc if fourier_calc is not None else maps.FourierCalc(shape[-2:],wcs)
    total = fc.f2power(icoadd,jcoadd)
    insplits = isplits.shape[0]
    jnsplits = jsplits.shape[0] 

    if alt:
        assert insplits==jnsplits
        noise = 0.
        for i in range(insplits):
            diff1 = isplits[i] - icoadd
            diff2 = jsplits[i] - jcoadd
            noise = noise + fc.f2power(diff1,diff2)
        noise = noise / ((1.-1./insplits)*insplits**2)
        crosses = total - noise
    else:
        ncrosses = 0.
        totcross = 0.
        for i in range(insplits):
            for j in range(jnsplits):
                if i==j: continue # FIXME: REALLY?! What about for independent experiments?
                totcross += fc.f2power(isplits[i],jsplits[j])
                ncrosses += 1.
        crosses = totcross / ncrosses
        noise = total - crosses

    return total,crosses,noise

    
    
        
def noise_from_splits(splits,fourier_calc=None,nthread=0,do_cross=True):
    """
    Calculate noise power spectra by subtracting cross power of splits 
    from autopower of splits. Optionally calculate cross power spectra
    of T,E,B from I,Q,U.

    splits -- (nsplits,ncomp,Ny,Nx) arrays

    ncomp can be 1 for T only, or 3 for I,Q,U
    ncomp could be > 3 for e.g. I1,Q1,U1,I2,Q2,U2 for 2 arrays

    """

    try:
        wcs = splits.wcs
    except:
        wcs = splits[0].wcs
        
    splits = enmap.enmap(np.asarray(splits),wcs).astype(np.float32)
    assert splits.ndim==3 or splits.ndim==4
    if splits.ndim == 3: splits = splits[:,None,:,:]
    ncomp = splits.shape[1]
    ndim = splits.ndim
        
    if fourier_calc is None:
        shape = splits.shape[-3:] if do_cross else splits.shape[-2:]
        fourier_calc = FourierCalc(shape,wcs)
    
    Nsplits = splits.shape[0]

    if do_cross: assert ncomp==3 or ncomp==1


    # Get fourier transforms of I,Q,U
    ksplits = [fourier_calc.iqu2teb(split, nthread=nthread, normalize=False, rot=False) for split in splits]
    del splits
    
    if do_cross:
        kteb_splits = []
        # Rotate I,Q,U to T,E,B for cross power (not necssary for noise)
        for ksplit in ksplits:
            kteb_splits.append( ksplit.copy())
            if (ndim==3 and ncomp==3):
                kteb_splits[-1][...,-2:,:,:] = enmap.map_mul(fourier_calc.rot, kteb_splits[-1][...,-2:,:,:])
            
    # get auto power of I,Q,U
    auto = 0.
    for ksplit in ksplits:
        auto += fourier_calc.power2d(kmap=ksplit)[0]
    auto /= Nsplits

    # do cross powers of I,Q,U
    Ncrosses = (Nsplits*(Nsplits-1)/2)
    cross = 0.
    for i in range(len(ksplits)):
        for j in range(i+1,len(ksplits)):
            cross += fourier_calc.power2d(kmap=ksplits[i],kmap2=ksplits[j])[0]
    cross /= Ncrosses
        
    if do_cross:
        # do cross powers of T,E,B
        cross_teb = 0.
        for i in range(len(ksplits)):
            for j in range(i+1,len(ksplits)):
                cross_teb += fourier_calc.power2d(kmap=kteb_splits[i],kmap2=kteb_splits[j])[0]
        cross_teb /= Ncrosses
    else:
        cross_teb = None
    del ksplits

    # get noise model for I,Q,U
    noise = (auto-cross)/Nsplits

    # return I,Q,U noise model and T,E,B cross-power
    return noise,cross_teb


### FULL SKY

class HealpixProjector(object):
    def __init__(self,shape,wcs,rot=None,ncomp=1):
        from pixell import coordinates
        self.pmap = enmap.posmap(shape, wcs)
        
        assert ncomp == 1 or ncomp == 3, "Only 1 or 3 components supported"
        pmap = self.pmap
        
        if rot:
            # Rotate by displacing coordinates and then fixing the polarization
            print("Computing pixel positions")
            if rot:
                print("Computing rotated positions")
                s1,s2 = rot.split(",")
                opos = coordinates.transform(s2, s1, pmap[::-1], pol=ncomp==3)
                pmap[...] = opos[1::-1]
                if len(opos) == 3: self.psi = -opos[2].copy()
                del opos
        self.ncomp = ncomp
        self.rot = rot
        
    def project(self,ihealmap=None,hpmap=None,unit=1,lmax=0,first=0,return_hp=False):
        from pixell import sharp, coordinates, curvedsky
        import healpy as hp

        dtype = np.float64
        ctype = np.result_type(dtype,0j)
        # Read the input maps
        print("Reading " + str(ihealmap))
        hpmap = hp.read_map(ihealmap, field=tuple(range(first,first+self.ncomp))) if hpmap is None else hpmap
        m = np.atleast_2d(hpmap).astype(dtype)
        if unit != 1: m /= unit
        # Prepare the transformation
        print("Preparing SHT")
        nside = hp.npix2nside(m.shape[1])
        lmax  = lmax or 3*nside
        minfo = sharp.map_info_healpix(nside)
        ainfo = sharp.alm_info(lmax)
        sht   = sharp.sht(minfo, ainfo)
        alm   = np.zeros((self.ncomp,ainfo.nelem), dtype=ctype)
        # Perform the actual transform
        print("T -> alm")
        print( m.dtype, alm.dtype)
        sht.map2alm(m[0], alm[0])
        if self.ncomp == 3:
            print("P -> alm")
            sht.map2alm(m[1:3],alm[1:3], spin=2)

        print("Projecting")
        res  = curvedsky.alm2map_pos(alm, self.pmap)
        if self.rot and self.ncomp==3:
            print("Rotating polarization vectors")
            res[1:3] = enmap.rotate_pol(res[1:3], self.psi)

        if return_hp:
            return res,m
        else:
            return res
        
def enmap_from_healpix_file(ihealmap,shape,wcs,ncomp=1,unit=1,lmax=0,rot_method="not-alm",rot=None,first=0):
    from pixell import utils, sharp, coordinates, curvedsky
    import healpy as hp
    
    # equatorial to galactic euler zyz angles
    euler = np.array([57.06793215,  62.87115487, -167.14056929])*utils.degree

    
    assert ncomp == 1 or ncomp == 3, "Only 1 or 3 components supported"
    dtype = np.float64
    ctype = np.result_type(dtype,0j)
    # Read the input maps
    print("Reading " + ihealmap)
    m = np.atleast_2d(hp.read_map(ihealmap, field=tuple(range(first,first+ncomp)))).astype(dtype)
    if unit != 1: m /= unit
    # Prepare the transformation
    print("Preparing SHT")
    nside = hp.npix2nside(m.shape[1])
    lmax  = lmax or 3*nside
    minfo = sharp.map_info_healpix(nside)
    ainfo = sharp.alm_info(lmax)
    sht   = sharp.sht(minfo, ainfo)
    alm   = np.zeros((ncomp,ainfo.nelem), dtype=ctype)
    # Perform the actual transform
    print("T -> alm")
    print( m.dtype, alm.dtype)
    sht.map2alm(m[0], alm[0])
    if ncomp == 3:
        print("P -> alm")
        sht.map2alm(m[1:3],alm[1:3], spin=2)
    del m


    if rot and rot_method != "alm":
        # Rotate by displacing coordinates and then fixing the polarization
        print("Computing pixel positions")
        pmap = enmap.posmap(shape, wcs)
        if rot:
            print("Computing rotated positions")
            s1,s2 = rot.split(",")
            opos = coordinates.transform(s2, s1, pmap[::-1], pol=ncomp==3)
            pmap[...] = opos[1::-1]
            if len(opos) == 3: psi = -opos[2].copy()
            del opos
        print("Projecting")
        res  = curvedsky.alm2map_pos(alm, pmap)
        if rot and ncomp==3:
            print("Rotating polarization vectors")
            res[1:3] = enmap.rotate_pol(res[1:3], psi)
    else:
        # We will project directly onto target map if possible
        if rot:
            print("Rotating alms")
            s1,s2 = rot.split(",")
            if s1 != s2:
                # Note: rotate_alm does not actually modify alm
                # if it is single precision
                if s1 == "gal" and (s2 == "equ" or s2 == "cel"):
                    hp.rotate_alm(alm, euler[0], euler[1], euler[2])
                elif s2 == "gal" and (s1 == "equ" or s1 == "cel"):
                    hp.rotate_alm(alm,-euler[2],-euler[1],-euler[0])
                else:
                    raise NotImplementedError
        print("Projecting")
        res = enmap.zeros((len(alm),)+shape[-2:], wcs, dtype)
        res = curvedsky.alm2map(alm, res)
    return res

# def enmap_from_healpix_alms(shape,wcs,hp_map_file=None,hp_map=None,ncomp=1,lmax=0,rot="gal,equ",rot_method="alm"):
#         """Project a healpix map to an enmap of chosen shape and wcs. The wcs
#         is assumed to be in equatorial (ra/dec) coordinates. If the healpix map
#         is in galactic coordinates, this can be specified by hp_coords, and a
#         slow conversion is done. No coordinate systems other than equatorial
#         or galactic are currently supported. Only intensity maps are supported.
#         If interpolate is True, bilinear interpolation using 4 nearest neighbours
#         is done.

#         shape -- 2-tuple (Ny,Nx)
#         wcs -- enmap wcs object in equatorial coordinates
#         hp_map -- array-like healpix map
#         hp_coords -- "galactic" to perform a coordinate transform, "fk5","j2000" or "equatorial" otherwise
#         interpolate -- boolean
        
#         """
        
#         import healpy
#         from enlib import coordinates, curvedsky, sharp, utils

#         # equatorial to galactic euler zyz angles
#         euler = np.array([57.06793215,  62.87115487, -167.14056929])*utils.degree

#         # If multiple templates are specified, the output file is
#         # interpreted as an output directory.

#         print("Loading map...")
#         assert ncomp == 1 or ncomp == 3, "Only 1 or 3 components supported"
#         dtype = np.float64
#         ctype = np.result_type(dtype,0j)
#         # Read the input maps
#         if hp_map_file is not None:
#             print(hp_map_file)
#             m = np.atleast_2d(healpy.read_map(hp_map_file, field=tuple(range(0,ncomp)))).astype(dtype)
#         else:
#             assert hp_map is not None
#             m = np.atleast_2d(hp_map).astype(dtype)

#         # Prepare the transformation
#         print("SHT prep...")

#         nside = healpy.npix2nside(m.shape[1])
#         lmax  = lmax or 3*nside
#         minfo = sharp.map_info_healpix(nside)
#         ainfo = sharp.alm_info(lmax)
#         sht   = sharp.sht(minfo, ainfo)
#         alm   = np.zeros((ncomp,ainfo.nelem), dtype=ctype)
#         # Perform the actual transform
#         print("SHT...")
#         sht.map2alm(m[0], alm[0])
        
#         if ncomp == 3:
#                 sht.map2alm(m[1:3],alm[1:3], spin=2)
#         del m


#         if rot and rot_method != "alm":
#                 print("rotate...")
#                 pmap = posmap(shape, wcs)
#                 s1,s2 = rot.split(",")
#                 opos = coordinates.transform(s2, s1, pmap[::-1], pol=ncomp==3)
#                 pmap[...] = opos[1::-1]
#                 if len(opos) == 3: psi = -opos[2].copy()
#                 del opos
#                 res  = curvedsky.alm2map_pos(alm, pmap)
#                 if ncomp==3:
#                         res[1:3] = rotate_pol(res[1:3], psi)
#         else:
#                 print(" alm rotate...")
#                 # We will project directly onto target map if possible
#                 if rot:
#                         s1,s2 = rot.split(",")
#                         if s1 != s2:
#                                 print("rotating alm...")
#                                 # Note: rotate_alm does not actually modify alm
#                                 # if it is single precision
#                                 if s1 == "gal" and (s2 == "equ" or s2 == "cel"):
#                                         healpy.rotate_alm(alm, euler[0], euler[1], euler[2])
#                                 elif s2 == "gal" and (s1 == "equ" or s1 == "cel"):
#                                         healpy.rotate_alm(alm,-euler[2],-euler[1],-euler[0])
#                                 else:
#                                         raise NotImplementedError
#                         print("done rotating alm...")
#                 res = enmap.zeros((len(alm),)+shape[-2:], wcs, dtype)
#                 res = curvedsky.alm2map(alm, res)
#         return res


# def enmap_from_healpix(shape,wcs,hp_map,hp_coords="galactic",interpolate=True):
#         """Project a healpix map to an enmap of chosen shape and wcs. The wcs
#         is assumed to be in equatorial (ra/dec) coordinates. If the healpix map
#         is in galactic coordinates, this can be specified by hp_coords, and a
#         slow conversion is done. No coordinate systems other than equatorial
#         or galactic are currently supported. Only intensity maps are supported.
#         If interpolate is True, bilinear interpolation using 4 nearest neighbours
#         is done.

#         shape -- 2-tuple (Ny,Nx)
#         wcs -- enmap wcs object in equatorial coordinates
#         hp_map -- array-like healpix map
#         hp_coords -- "galactic" to perform a coordinate transform, "fk5","j2000" or "equatorial" otherwise
#         interpolate -- boolean
        
#         """
        
#         import healpy as hp
#         from astropy.coordinates import SkyCoord
#         import astropy.units as u


#         eq_coords = ['fk5','j2000','equatorial']
#         gal_coords = ['galactic']
        
#         imap = enmap.zeros(shape,wcs)
#         Ny,Nx = shape

#         pixmap = enmap.pixmap(shape,wcs)
#         y = pixmap[0,...].T.ravel()
#         x = pixmap[1,...].T.ravel()
#         posmap = enmap.posmap(shape,wcs)

#         ph = posmap[1,...].T.ravel()
#         th = posmap[0,...].T.ravel()

#         if hp_coords.lower() not in eq_coords:
#                 # This is still the slowest part. If there are faster coord transform libraries, let me know!
#                 assert hp_coords.lower() in gal_coords
#                 gc = SkyCoord(ra=ph*u.degree, dec=th*u.degree, frame='fk5')
#                 gc = gc.transform_to('galactic')
#                 phOut = gc.l.deg* np.pi/180.
#                 thOut = gc.b.deg* np.pi/180.
#         else:
#                 thOut = th
#                 phOut = ph

#         thOut = np.pi/2. - thOut #polar angle is 0 at north pole

#         # Not as slow as you'd expect
#         if interpolate:
#                 imap[y,x] = hp.get_interp_val(hp_map, thOut, phOut)
#         else:
#                 ind = hp.ang2pix( hp.get_nside(hp_map), thOut, phOut )
#                 imap[:] = 0.
#                 imap[[y,x]]=hp_map[ind]

                
                
#         return enmap.ndmap(imap,wcs)


def get_planck_cutout(imap,ra,dec,arcmin,px=2.0,arcmin_y=None):
    if arcmin_y is None: arcmin_y = arcmin
    xsize = int(arcmin/px)
    ysize = int(arcmin_y/px)
    shape,wcs = enmap.geometry(pos=(0,0),shape=(ysize,xsize),res=np.deg2rad(px/60.))
    return enmap.enmap(cutout_gnomonic(imap,rot=(ra,dec),coord=['G','C'],
                    xsize=xsize,ysize=ysize,reso=px,gal_cut=0,flip='geo'),wcs)
    
def cutout_gnomonic(map,rot=None,coord=None,
             xsize=200,ysize=None,reso=1.5,
             nest=False,remove_dip=False,
             remove_mono=False,gal_cut=0,
             flip='astro'):
    """Obtain a cutout from a healpix map (given as an array) in Gnomonic projection.

    Derivative of healpy.visufunc.gnomonic

    Parameters
    ----------
    map : array-like
      The map to project, supports masked maps, see the `ma` function.
    rot : scalar or sequence, optional
      Describe the rotation to apply.
      In the form (lon, lat, psi) (unit: degrees) : the point at
      longitude *lon* and latitude *lat* will be at the center. An additional rotation
      of angle *psi* around this direction is applied.
    coord : sequence of character, optional
      Either one of 'G', 'E' or 'C' to describe the coordinate
      system of the map, or a sequence of 2 of these to rotate
      the map from the first to the second coordinate system.
    xsize : int, optional
      The size of the image. Default: 200
    ysize : None or int, optional
      The size of the image. Default: None= xsize
    reso : float, optional
      Resolution (in arcmin). Default: 1.5 arcmin
    nest : bool, optional
      If True, ordering scheme is NESTED. Default: False (RING)
    flip : {'astro', 'geo'}, optional
      Defines the convention of projection : 'astro' (default, east towards left, west towards right)
      or 'geo' (east towards roght, west towards left)
    remove_dip : bool, optional
      If :const:`True`, remove the dipole+monopole
    remove_mono : bool, optional
      If :const:`True`, remove the monopole
    gal_cut : float, scalar, optional
      Symmetric galactic cut for the dipole/monopole fit.
      Removes points in latitude range [-gal_cut, +gal_cut]
    

    See Also
    --------
    gnomview, mollview, cartview, orthview, azeqview
    """
    import pylab
    import healpy as hp
    import healpy.projaxes as PA

    margins = (0.075,0.05,0.075,0.05)
    extent = (0.0,0.0,1.0,1.0)
    extent = (extent[0]+margins[0],
              extent[1]+margins[1],
              extent[2]-margins[2]-margins[0],
              extent[3]-margins[3]-margins[1])
    f=pylab.figure(0,figsize=(5.5,6))
    map = hp.pixelfunc.ma_to_array(map)
    ax=PA.HpxGnomonicAxes(f,extent,coord=coord,rot=rot,
                          format="%.3g",flipconv=flip)
    if remove_dip:
        map=hp.pixelfunc.remove_dipole(map,gal_cut=gal_cut,nest=nest,copy=True)
    elif remove_mono:
        map=hp.pixelfunc.remove_monopole(map,gal_cut=gal_cut,nest=nest,copy=True)
    img = ax.projmap(map,nest=nest,coord=coord,
               xsize=xsize,ysize=ysize,reso=reso)

    pylab.close(f)
    return img


def whiteNoise2D(noiseLevels,beamArcmin,modLMap,TCMB = 2.7255e6,lknees=None,alphas=None,beamFile=None, \
                 noiseFuncs=None):
    # Returns 2d map noise in units of uK**0.
    # Despite the name of the function, there are options to add
    # a simplistic atmosphere noise model

    # If no atmosphere is specified, set lknee to zero and alpha to 1
    if lknees is None:
        lknees = (np.array(noiseLevels)*0.).tolist()
    if alphas is None:
        alphas = (np.array(noiseLevels)*0.+1.).tolist()

    # we'll loop over it, so make it a list if nothing is specified
    if noiseFuncs is None: noiseFuncs = [None]*len(noiseLevels)

        
    # if one of the noise files is not specified, we will need a beam
    if None in noiseFuncs:
        
        if beamFile is not None:
            ell, f_ell = np.transpose(np.loadtxt(beamFile))[0:2,:]
            filt = 1./(np.array(f_ell)**2.)
            bfunc = interp1d(ell,f_ell,bounds_error=False,fill_value=np.inf)
            filt2d = bfunc(modLMap)
        else:
            Sigma = beamArcmin *np.pi/60./180./ np.sqrt(8.*np.log(2.))  # radians
            filt2d = np.exp(-(modLMap**2.)*Sigma*Sigma)

    retList = []

    for noiseLevel,lknee,alpha,noiseFunc in zip(noiseLevels,lknees,alphas,noiseFuncs):
        if noiseFunc is not None:
            retList.append(noiseFunc(modLMap))
        else:
            noiseForFilter = (np.pi / (180. * 60.))**2.  * noiseLevel**2. / TCMB**2.  
            if lknee>1.e-3:
                atmFactor = (lknee*np.nan_to_num(1./modLMap))**(-alpha)
            else:
                atmFactor = 0.
                
            with np.errstate(divide='ignore'):
                retList.append(noiseForFilter*(atmFactor+1.)*np.nan_to_num(1./filt2d.copy()))
    return retList


### INTERFACES WITH EXPERIMENTS



class ACTMapReader(object):

    def __init__(self,map_root,beam_root,config_yaml_path):

        with open(config_yaml_path, 'r') as ymlfile:
            self._cfg = yaml.load(ymlfile,Loader=yaml.BaseLoader)

        self.map_root = map_root #self._cfg['map_root']
        self.beam_root = beam_root #self._cfg['beam_root']

        
        self.boxes = {}
        for key in self._cfg['patches']:
            self.boxes[key] = bounds_from_list(io.list_from_string(self._cfg['patches'][key]))


    def sel_from_region(self,region,shape=None,wcs=None):
        if shape is None: shape = self.shape
        if wcs is None: wcs = self.wcs
        if region is None:
            selection = None
        elif isinstance(region, six.string_types):
            selection = self.boxes[region] #enmap.slice_from_box(shape,wcs,self.boxes[region])
        else:
            selection = region #enmap.slice_from_box(shape,wcs,region)
        return selection
    
    def patch_bounds(self,patch):
        return (np.array([float(x) for x in self._cfg['patches'][patch].split(',')])*np.pi/180.).reshape((2,2))
        


class SigurdCoaddReader(ACTMapReader):
    
    def __init__(self,map_root,beam_root,config_yaml_path):
        ACTMapReader.__init__(self,map_root,beam_root,config_yaml_path)
        eg_file = self._fstring(split=-1,freq="150",day_night="daynight",planck=True)
        self.shape,self.wcs = enmap.read_fits_geometry(eg_file)


    def _config_tag(self,freq,day_night,planck):
        planckstr = "_planck" if planck else ""
        return freq+planckstr+"_"+day_night


    def get_ptsrc_mask(self,region=None):
        selection = self.sel_from_region(region)
        fstr = self.map_root+"s16/coadd/pointSourceMask_full_all.fits"
        fmap = enmap.read_fits(fstr,box=selection)
        return fmap
    
    def get_survey_mask(self,region=None):
        selection = self.sel_from_region(region)
        self.map_root+"s16/coadd/surveyMask_full_all.fits"
        fmap = enmap.read_fits(fstr,box=selection)
        return fmap
    
    def get_map(self,split,freq="150",day_night="daynight",planck=True,region=None,weight=False,get_identifier=False):

        
        fstr = self._fstring(split,freq,day_night,planck,weight)
        cal = float(self._cfg['coadd'][self._config_tag(freq,day_night,planck)]['cal']) if not(weight) else 1.

        selection = self.sel_from_region(region)
        fmap = enmap.read_fits(fstr,box=selection)*np.sqrt(cal)

        if get_identifier:
            identifier = '_'.join(map(str,[freq,day_night,"planck",planck]))
            return fmap,identifier
        else:
            return fmap
        

    def _fstring(self,split,freq="150",day_night="daynight",planck=True,weight=False):
        # Change this function if the map naming scheme changes
        splitstr = "" if split<0 or split>3 else "_2way_"+str(split)
        weightstr = "div" if weight else "map"
        return self.map_root+"s16/coadd/f"+freq+"_"+day_night+"_all"+splitstr+"_"+weightstr+"_mono.fits"


    def get_beam(self,freq="150",day_night="daynight",planck=True):
        beam_file = self.beam_root+self._cfg['coadd'][self._config_tag(freq,day_night,planck)]['beam']
        ls,bells = np.loadtxt(beam_file,usecols=[0,1],unpack=True)
        return ls, bells


class SigurdMR2Reader(ACTMapReader):
    
    def __init__(self,map_root,beam_root,config_yaml_path):
        ACTMapReader.__init__(self,map_root,beam_root,config_yaml_path)
        eg_file = self._fstring(split=-1,season="s15",array="pa1",freq="150",day_night="night")
        self.shape,self.wcs = enmap.read_fits_geometry(eg_file)

    def get_map(self,split,season,array,freq="150",day_night="night",region=None,weight=False,get_identifier=False):

        
        patch="boss"
        fstr = self._fstring(split,season,array,freq,day_night,weight)
        cal = float(self._cfg[season][array][freq][patch][day_night]['cal']) if not(weight) else 1.
        selection = self.sel_from_region(region)
        fmap = enmap.read_fits(fstr,box=selection)*np.sqrt(cal)

        if weight:
            ndim = fmap.ndim
            if ndim==4: fmap = fmap[0,0]
            if ndim==3: fmap = fmap[0]
            

        if get_identifier:
            identifier = '_'.join(map(str,[freq,day_night,"planck",planck]))
            return fmap,identifier
        else:
            return fmap
        
    def _fstring(self,split,season,array,freq="150",day_night="night",weight=False):
        # Change this function if the map naming scheme changes
        splitstr = "_4way_tot_" if split<0 or split>3 else "_4way_"+str(split)+"_"
        weightstr = "div" if weight else "map0500"
        return self.map_root+"mr2/"+season+"/boss_north/"+season+"_boss_"+array+"_f"+freq.zfill(3)+"_"+day_night+"_nohwp"+splitstr+"sky_"+weightstr+"_mono.fits"


    def get_beam(self,season,array,freq="150",day_night="night"):
        patch = "boss"
        beam_file = self.beam_root+self._cfg[season][array][freq][patch][day_night]['beam']
        ls,bells = np.loadtxt(beam_file,usecols=[0,1],unpack=True)
        return ls, bells

    
class SimoneMR2Reader(ACTMapReader):
    
    def __init__(self,map_root,beam_root,config_yaml_path,patch):
        ACTMapReader.__init__(self,map_root,beam_root,config_yaml_path)
        self.patch = patch
        #eg_file = self._fstring(split=-1,season="s15" if (patch=="deep56" or patch=="deep8") else "s13",array="pa1",freq="150",day_night="night",pol="I")
        eg_file = self._fstring(split=-1,season="s14" if (patch=="deep56" or patch=="deep8") else "s13",array="pa2" if (patch=="deep56" or patch=="deep8") else "pa1",freq="150",day_night="night",pol="srcfree_I")
        self.shape,self.wcs = enmap.read_fits_geometry(eg_file)
        
    def get_beam(self,season,array,freq="150",day_night="night"):
        patch = self.patch
        beam_file = self.beam_root+self._cfg[season][array][freq][patch][day_night]['beam']
        ls,bells = np.loadtxt(beam_file,usecols=[0,1],unpack=True)
        return ls, bells
    def get_map(self,split,season,array,freq="150",day_night="night",full_map=True,weight=False,get_identifier=False,t_only=False,box=None):
        patch = self.patch
        maps = []
        maplist = ['srcfree_I','Q','U'] if not(t_only) else ['srcfree_I']
        for pol in maplist if not(weight) else [None]:
            fstr = self._hstring(season,array,freq,day_night) if weight else self._fstring(split,season,array,freq,day_night,pol)
            cal = float(self._cfg[season][array][freq][patch][day_night]['cal']) #if not(weight) else 1.
            fmap = enmap.read_fits(fstr,box=box)*np.sqrt(cal) if not(weight) else np.nan_to_num(1./enmap.read_fits(fstr,box=box)**2.)
            if not(full_map):
                bounds = self.patch_bounds(patch) 
                retval = fmap.submap(bounds)
            else:
                retval = fmap
            maps.append(retval)
        retval = enmap.ndmap(np.stack(maps),maps[0].wcs) if not(weight) else maps[0]

        if get_identifier:
            identifier = '_'.join(map(str,[season,patch,array,freq,day_night]))
            return retval,identifier
        else:
            return retval
        

    def _fstring(self,split,season,array,freq,day_night,pol):
        patch = self.patch
        # Change this function if the map naming scheme changes
        splitstr = "set0123" if split<0 or split>3 else "set"+str(split)
        #return self.map_root+"c7v5/"+season+"/"+patch+"/"+season+"_mr2_"+patch+"_"+array+"_f"+freq+"_"+day_night+"_"+splitstr+"_wpoly_500_"+pol+".fits"
        return self.map_root+"mr2/"+season+"/"+patch+"/"+season+"_mr2_"+patch+"_"+array+"_f"+freq+"_"+day_night+"_"+splitstr+"_wpoly_500_"+pol+".fits"

    def _hstring(self,season,array,freq,day_night):
        patch = self.patch
        splitstr = "set0123"
        #return self.map_root+"c7v5/"+season+"/"+patch+"/"+season+"_mr2_"+patch+"_"+array+"_f"+freq+"_"+day_night+"_"+splitstr+"_hits.fits"
        #return self.map_root+"mr2/"+season+"/"+patch+"/"+season+"_mr2_"+patch+"_"+array+"_f"+freq+"_"+day_night+"_"+splitstr+"_hits.fits"
        return self.map_root+"mr2/"+season+"/"+patch+"/"+season+"_mr2_"+patch+"_"+array+"_f"+freq+"_"+day_night+"_"+splitstr+"_noise.fits"



### STACKING

class Stacker(object):

    def __init__(self,imap,arcmin_width):
        self.imap = imap
        res = np.min(imap.extent()/imap.shape[-2:])*180./np.pi*60.
        self.Npix = int(arcmin_width/res)*1.
        if self.Npix%2==0: self.Npix += 1
        self.shape,self.wcs = enmap.geometry(pos=(0.,0.),res=res/(180./np.pi*60.),shape=(self.Npix,self.Npix))

    def cutout(self,ra,dec):   
        iy,ix = self.imap.sky2pix(coords=(dec,ra))
        cutout = self.imap[int(iy-self.Npix/2):int(iy+self.Npix/2),int(ix-self.Npix/2):int(ix+self.Npix/2)]
        assert self.shape==cutout.shape
        return enmap.ndmap(cutout,self.wcs)


def cutout_slice(shape,wcs,arcmin_width=None,ra=None,dec=None,iy=None,ix=None,pad=1,corner=False,res=None,Npix=None):
    Ny,Nx = shape
    # see enmap.sky2pix for "corner" options
    if corner:
        fround = lambda x : int(math.floor(x))
    else:
        fround = lambda x : int(np.round(x))
    #fround = lambda x : int(x)
    if (iy is None) or (ix is None):
        iy,ix = enmap.sky2pix(shape,wcs,coords=(dec,ra),corner=corner)
    if res is None:
        res = np.min(enmap.extent(shape,wcs)/shape[-2:])*180./np.pi*60.
    else:
        res = res*180./np.pi*60.
    
    if Npix is None: Npix = int(arcmin_width/res)
    if fround(iy-Npix/2)<pad or fround(ix-Npix/2)<pad or fround(iy+Npix/2)>(Ny-pad) or fround(ix+Npix/2)>(Nx-pad): return None
    s = np.s_[fround(iy-Npix/2.+0.5):fround(iy+Npix/2.+0.5),fround(ix-Npix/2.+0.5):fround(ix+Npix/2.+0.5)]
    return s,res,Npix
    
def cutout(imap,arcmin_width=None,ra=None,dec=None,iy=None,ix=None,pad=1,corner=False,preserve_wcs=False,res=None,Npix=None,proj="car"):
    ret = cutout_slice(imap.shape,imap.wcs,arcmin_width=arcmin_width,ra=ra,dec=dec,iy=iy,ix=ix,pad=pad,corner=corner,res=res,Npix=Npix)
    if ret is None:
        return None
    s,res,Npix = ret
    cutout = imap[s]
    if preserve_wcs:
        return cutout
    else:
        shape,wcs = enmap.geometry(pos=(0.,0.),res=res/(180./np.pi*60.),shape=(Npix,Npix),proj=proj)
        assert np.all(cutout.shape==shape)
        return enmap.enmap(cutout,wcs)


def aperture_photometry(instamp,aperture_radius,annulus_width,modrmap=None):
    # inputs in radians, outputs in arcmin^2
    stamp = instamp.copy()
    if modrmap is None: modrmap = stamp.modrmap()
    mean = stamp[np.logical_and(modrmap>aperture_radius,modrmap<(aperture_radius+annulus_width))].mean()
    stamp -= mean
    pix_scale=resolution(stamp.shape,stamp.wcs)*(180*60)/np.pi
    flux = stamp[modrmap<aperture_radius].sum()*pix_scale**2
    return flux #* enmap.area(stamp.shape,stamp.wcs )/ np.prod(stamp.shape[-2:])**2.  *((180*60)/np.pi)**2.




class InterpStack(object):

    def __init__(self,arc_width,px,proj="car"):
        if proj.upper()!="CAR": raise NotImplementedError
        
        self.shape_target,self.wcs_target = rect_geometry(width_arcmin=arc_width,
                                                      height_arcmin=arc_width,
                                                      px_res_arcmin=px,yoffset_degree=0.
                                                      ,xoffset_degree=0.,proj=proj)
                 
        center_target = enmap.pix2sky(self.shape_target,self.wcs_target,(self.shape_target[0]/2.,self.shape_target[1]/2.))
        self.dect,self.rat = center_target
        # what are the angle coordinates of each pixel in the target geometry
        pos_target = enmap.posmap(self.shape_target,self.wcs_target)
        self.lra = pos_target[1,:,:].ravel()
        self.ldec = pos_target[0,:,:].ravel()
        del pos_target

        self.arc_width = arc_width 


    def cutout(self,imap,ra,dec,**kwargs):
        ra_rad = np.deg2rad(ra)
        dec_rad = np.deg2rad(dec)

        box = self._box_from_ra_dec(ra_rad,dec_rad)
        submap = imap.submap(box,inclusive=True)
        return self._rot_cut(submap,ra_rad,dec_rad,**kwargs)
    
    def cutout_from_file(self,imap_file,shape,wcs,ra,dec,**kwargs):
        ra_rad = np.deg2rad(ra)
        dec_rad = np.deg2rad(dec)

        box = self._box_from_ra_dec(ra_rad,dec_rad)
        # print(ra_rad*180./np.pi,dec_rad*180./np.pi,box*180./np.pi)
        # selection = slice_from_box(shape,wcs,box)
        # print(selection)
        submap = enmap.read_fits(imap_file,box=box)#sel=selection)
        print(submap.shape)
        # sys.exit()
        # submap = enmap.read_fits(imap_file,sel=selection)
        # io.plot_img(submap,io.dout_dir+"scut.png",high_res=True)

        # try:
        #     self.count +=1
        # except:
        #     self.count = 0

        # print(ra,dec)
        # io.plot_img(submap,io.dout_dir+"stest_qwert_"+str(self.count)+".png")


        
        return self._rot_cut(submap,ra_rad,dec_rad,**kwargs)

    def _box_from_ra_dec(self,ra_rad,dec_rad):

        
        # CAR
        coord_width = np.deg2rad(self.arc_width/60.)#np.cos(dec_rad)/60.)
        coord_height = np.deg2rad(self.arc_width/60.)

        box = np.array([[dec_rad-coord_height/2.,ra_rad-coord_width/2.],[dec_rad+coord_height/2.,ra_rad+coord_width/2.]])

        return box

        
    def _rot_cut(self,submap,ra_rad,dec_rad,**kwargs):
        from pixell import coordinates
    
        if submap.shape[0]<1 or submap.shape[1]<1:
            return None
        
        
        newcoord = coordinates.recenter((self.lra,self.ldec),(self.rat,self.dect,ra_rad,dec_rad))

        # reshape these new coordinates into enmap-friendly form
        new_pos = np.empty((2,self.shape_target[0],self.shape_target[1]))
        new_pos[0,:,:] = newcoord[1,:].reshape(self.shape_target)
        new_pos[1,:,:] = newcoord[0,:].reshape(self.shape_target)
        del newcoord
        
        # translate these new coordinates to pixel positions in the target geometry based on the source's wcs
        pix_new = enmap.sky2pix(submap.shape,submap.wcs,new_pos)

        rotmap = enmap.at(submap,pix_new,unit="pix",**kwargs)
        assert rotmap.shape[-2:]==self.shape_target[-2:]

        
        return rotmap
        





def interpolate_grid(inGrid,inY,inX,outY=None,outX=None,regular=True,kind="cubic",kx=3,ky=3,**kwargs):
    '''
    if inGrid is [j,i]
    Assumes inY is along j axis
    Assumes inX is along i axis
    Similarly for outY/X
    '''

    if regular:
        interp_spline = RectBivariateSpline(inY,inX,inGrid,kx=kx,ky=ky,**kwargs)
        if (outY is None) and (outX is None): return interp_spline
        outGrid = interp_spline(outY,outX)
    else:
        interp_spline = interp2d(inX,inY,inGrid,kind=kind,**kwargs)
        if (outY is None) and (outX is None): return lambda y,x: interp_spline(x,y)
        outGrid = interp_spline(outX,outY)
    

    return outGrid
    


class MatchedFilter(object):

    def __init__(self,shape,wcs,template=None,noise_power=None):
        shape = shape[-2:]
        area = enmap.area(shape,wcs)
        self.normfact = area / (np.prod(shape))**2
        if noise_power is not None: self.n2d = noise_power
        if template is not None: self.ktemp = enmap.fft(template,normalize=False)

        

    def apply(self,imap=None,kmap=None,template=None,ktemplate=None,noise_power=None,kmask=None):
        if kmap is None:
            kmap = enmap.fft(imap,normalize=False)
        else:
            assert imap is None

        if kmask is None: kmask = kmap.copy()*0.+1.
        n2d = self.n2d if noise_power is None else noise_power
        if ktemplate is None:
            ktemp = self.ktemp if template is None else enmap.fft(template,normalize=False)
        else:
            ktemp = ktemplate
            
        phi_un = np.nansum(ktemp.conj()*kmap*self.normfact*kmask/n2d).real 
        phi_var = 1./np.nansum(ktemp.conj()*ktemp*self.normfact*kmask/n2d).real 
        return phi_un*phi_var, phi_var



def mask_center(inmap):
    imap = inmap.copy()
    Ny,Nx = imap.shape
    assert Ny==Nx
    N = Ny
    if N%2==1:
        imap[N//2,N//2] = np.nan
    else:
        imap[N//2,N//2] = np.nan
        imap[N//2-1,N//2] = np.nan
        imap[N//2,N//2-1] = np.nan
        imap[N//2-1,N//2-1] = np.nan

    return imap

        
class Purify(object):

    def __init__(self,shape,wcs,window):
        px = resolution(shape,wcs)
        self.windict = init_deriv_window(window,px)
        lxMap,lyMap,self.modlmap,self.angLMap,lx,ly = get_ft_attributes(shape,wcs)

    def lteb_from_iqu(self,imap,method='pure',flip_q=True,iau=False):
        """
        maps must  have window applied!
        """
        sgnq = -1 if flip_q else 1
        fT, fE, fB = iqu_to_pure_lteb(imap[0],sgnq*imap[1],imap[2],self.modlmap,self.angLMap,windowDict=self.windict,method=method,iau=iau)
        return fT,-fE,-fB
        



def init_deriv_window(window,px):
    """
    px is in radians
    """
	
    def matrixShift(l,row_shift,column_shift):	
        m1=np.hstack((l[:,row_shift:],l[:,:row_shift]))
        m2=np.vstack((m1[column_shift:],m1[:column_shift]))
        return m2
    delta=px
    Win=window[:]
    
    dWin_dx=(-matrixShift(Win,-2,0)+8*matrixShift(Win,-1,0)-8*matrixShift(Win,1,0)+matrixShift(Win,2,0))/(12*delta)
    dWin_dy=(-matrixShift(Win,0,-2)+8*matrixShift(Win,0,-1)-8*matrixShift(Win,0,1)+matrixShift(Win,0,2))/(12*delta)
    d2Win_dx2=(-matrixShift(dWin_dx,-2,0)+8*matrixShift(dWin_dx,-1,0)-8*matrixShift(dWin_dx,1,0)+matrixShift(dWin_dx,2,0))/(12*delta)
    d2Win_dy2=(-matrixShift(dWin_dy,0,-2)+8*matrixShift(dWin_dy,0,-1)-8*matrixShift(dWin_dy,0,1)+matrixShift(dWin_dy,0,2))/(12*delta)
    d2Win_dxdy=(-matrixShift(dWin_dy,-2,0)+8*matrixShift(dWin_dy,-1,0)-8*matrixShift(dWin_dy,1,0)+matrixShift(dWin_dy,2,0))/(12*delta)
    
    #In return we change the sign of the simple gradient in order to agree with np convention
    return {'Win':Win, 'dWin_dx':-dWin_dx,'dWin_dy':-dWin_dy, 'd2Win_dx2':d2Win_dx2, 'd2Win_dy2':d2Win_dy2,'d2Win_dxdy':d2Win_dxdy}
	



def iqu_to_pure_lteb(T_map,Q_map,U_map,modLMap,angLMap,windowDict,method='pure',iau=False):
    """
    maps must  have window applied!
    """

    if iau: angLMap = -angLMap
    window = windowDict

    win =window['Win']
    dWin_dx=window['dWin_dx']
    dWin_dy=window['dWin_dy']
    d2Win_dx2=window['d2Win_dx2'] 
    d2Win_dy2=window['d2Win_dy2']
    d2Win_dxdy=window['d2Win_dxdy']

    T_temp=T_map.copy() #*win
    fT=fft(T_temp,axes=[-2,-1])
    
    Q_temp=Q_map.copy() #*win
    fQ=fft(Q_temp,axes=[-2,-1])
    
    U_temp=U_map.copy() #*win
    fU=fft(U_temp,axes=[-2,-1])
    
    fE=fT.copy()
    fB=fT.copy()
    
    fE=fQ[:]*np.cos(2.*angLMap)+fU[:]*np.sin(2.*angLMap)
    fB=-fQ[:]*np.sin(2.*angLMap)+fU[:]*np.cos(2.*angLMap)
    
    if method=='standard':
        return fT, fE, fB
    
    Q_temp=Q_map.copy()*dWin_dx
    QWx=fft(Q_temp,axes=[-2,-1])
    
    Q_temp=Q_map.copy()*dWin_dy
    QWy=fft(Q_temp,axes=[-2,-1])
    
    U_temp=U_map.copy()*dWin_dx
    UWx=fft(U_temp,axes=[-2,-1])
    
    U_temp=U_map.copy()*dWin_dy
    UWy=fft(U_temp,axes=[-2,-1])
    
    U_temp=2.*Q_map*d2Win_dxdy-U_map*(d2Win_dx2-d2Win_dy2)
    QU_B=fft(U_temp,axes=[-2,-1])
 
    U_temp=-Q_map*(d2Win_dx2-d2Win_dy2)-2.*U_map*d2Win_dxdy
    QU_E=fft(U_temp,axes=[-2,-1])
    
    modLMap=modLMap+2


    fB[:] += QU_B[:]*(1./modLMap)**2
    fB[:]-= (2.*1j)/modLMap*(np.sin(angLMap)*(QWx[:]+UWy[:])+np.cos(angLMap)*(QWy[:]-UWx[:]))
    
    if method=='hybrid':
        return fT, fE, fB
    
    fE[:]+= QU_E[:]*(1./modLMap)**2
    fE[:]-= (2.*1j)/modLMap*(np.sin(angLMap)*(QWy[:]-UWx[:])-np.cos(angLMap)*(QWx[:]+UWy[:]))
    
    if method=='pure':
        return fT, fE, fB



## LEGACY

class PatchArray(object):
    def __init__(self,shape,wcs,dimensionless=False,TCMB=2.7255e6,cc=None,theory=None,lmax=None,skip_real=False,orphics_is_dimensionless=True):
        self.shape = shape
        self.wcs = wcs
        if not(skip_real): self.modrmap = enmap.modrmap(shape,wcs)
        self.lxmap,self.lymap,self.modlmap,self.angmap,self.lx,self.ly = get_ft_attributes(shape,wcs)
        self.pix_ells = np.arange(0.,self.modlmap.max(),1.)
        self.posmap = enmap.posmap(self.shape,self.wcs)
        self.dimensionless = dimensionless
        self.TCMB = TCMB

        if (theory is not None) or (cc is not None):
            self.add_theory(cc,theory,lmax,orphics_is_dimensionless)

    def add_theory(self,cc=None,theory=None,lmax=None,orphics_is_dimensionless=True):
        if cc is not None:
            self.cc = cc
            self.theory = cc.theory
            self.lmax = cc.lmax
            # assert theory is None
            # assert lmax is None
            if theory is None: theory = self.theory
            if lmax is None: lmax = self.lmax
        else:
            assert theory is not None
            assert lmax is not None
            self.theory = theory
            self.lmax = lmax
            
        #psl = cmb.enmap_power_from_orphics_theory(theory,lmax,lensed=True,dimensionless=self.dimensionless,TCMB=self.TCMB,orphics_dimensionless=orphics_is_dimensionless)
        from orphics import cosmology
        psu = cosmology.enmap_power_from_orphics_theory(theory,lmax,lensed=False,dimensionless=self.dimensionless,TCMB=self.TCMB,orphics_dimensionless=orphics_is_dimensionless)
        self.fine_ells = np.arange(0,lmax,1)
        pclkk = theory.gCl("kk",self.fine_ells)
        self.clkk = pclkk.copy()
        pclkk = pclkk.reshape((1,1,pclkk.size))
        #self.pclkk.resize((1,self.pclkk.size))
        self.ugenerator = MapGen(self.shape,self.wcs,psu)
        self.kgenerator = MapGen(self.shape[-2:],self.wcs,pclkk)


    def update_kappa(self,kappa):
        # Converts kappa map to pixel displacements
        from orphics import lensing
        fphi = lensing.kappa_to_fphi(kappa,self.modlmap)
        grad_phi = enmap.gradf(enmap.ndmap(fphi,self.wcs))
        pos = self.posmap + grad_phi
        self._displace_pix = enmap.sky2pix(self.shape,self.wcs,pos, safe=False)

    def get_lensed(self, unlensed, order=3, mode="spline", border="cyclic"):
        from pixell import lensing as enlensing
        return enlensing.displace_map(unlensed, self._displace_pix, order=order, mode=mode, border=border)


    def get_unlensed_cmb(self,seed=None,scalar=False):
        return self.ugenerator.get_map(seed=seed,scalar=scalar)
        
    def get_grf_kappa(self,seed=None,skip_update=False):
        kappa = self.kgenerator.get_map(seed=seed,scalar=True)
        if not(skip_update): self.update_kappa(kappa)
        return kappa


    def _fill_beam(self,beam_func):
        self.lbeam = beam_func(self.modlmap)
        self.lbeam[self.modlmap<2] = 1.
        
    def add_gaussian_beam(self,fwhm):
        if fwhm<1.e-5:
            bfunc = lambda x: x*0.+1.
        else:
            bfunc = lambda x : cmb.gauss_beam(x,fwhm)
        self._fill_beam(bfunc)
        
    def add_1d_beam(self,ells,bls,fill_value="extrapolate"):
        bfunc = interp1d(ells,bls,fill_value=fill_value)
        self._fill_beam(bfunc)

    def add_2d_beam(self,beam_2d):
        self.lbeam = beam_2d

    def add_white_noise_with_atm(self,noise_uK_arcmin_T,noise_uK_arcmin_P=None,lknee_T=0.,alpha_T=0.,lknee_P=0.,
                        alpha_P=0.):

        map_dimensionless=self.dimensionless
        TCMB=self.TCMB


        
        self.nT = cmb.white_noise_with_atm_func(self.modlmap,noise_uK_arcmin_T,lknee_T,alpha_T,
                                                map_dimensionless,TCMB)
        
        if noise_uK_arcmin_P is None and np.isclose(lknee_T,lknee_P) and np.isclose(alpha_T,alpha_P):
            self.nP = 2.*self.nT.copy()
        else:
            if noise_uK_arcmin_P is None: noise_uK_arcmin_P = np.sqrt(2.)*noise_uK_arcmin_T
            self.nP = cmb.white_noise_with_atm_func(self.modlmap,noise_uK_arcmin_P,lknee_P,alpha_P,
                                      map_dimensionless,TCMB)

        TCMBt = TCMB if map_dimensionless else 1.
        ps_noise = np.zeros((3,3,self.pix_ells.size))
        ps_noise[0,0] = self.pix_ells*0.+(noise_uK_arcmin_T*np.pi/180./60./TCMBt)**2.
        ps_noise[1,1] = self.pix_ells*0.+(noise_uK_arcmin_P*np.pi/180./60./TCMBt)**2.
        ps_noise[2,2] = self.pix_ells*0.+(noise_uK_arcmin_P*np.pi/180./60./TCMBt)**2.
        noisecov = ps_noise
        self.is_2d_noise = False
        self.ngenerator = MapGen(self.shape,self.wcs,noisecov)

            
    def add_noise_2d(self,nT,nP=None):
        self.nT = nT
        if nP is None: nP = 2.*nT
        self.nP = nP

        ps_noise = np.zeros((3,3,self.modlmap.shape[0],self.modlmap.shape[1]))
        ps_noise[0,0] = nT
        ps_noise[1,1] = nP
        ps_noise[2,2] = nP
        noisecov = ps_noise
        self.is_2d_noise = True
        self.ngenerator = MapGen(self.shape,self.wcs,noisecov)


    def get_noise_sim(self,seed=None,scalar=False):
        return self.ngenerator.get_map(seed=seed,scalar=scalar)
    



def gauss_kern(sigmaY,sigmaX,nsigma=5.0):
    """
    @ brief Returns a normalized 2D gauss kernel array for convolutions
    ^
    | Y
    |
    ------>
      X 
    """
    sizeY = int(nsigma*sigmaY)
    sizeX = int(nsigma*sigmaX)
    
    y, x = np.mgrid[-sizeY:sizeY+1, -sizeX:sizeX+1]
    g = np.exp(-(x**2/(2.*sigmaX**2)+y**2/(2.*sigmaY**2)))
    return g / g.sum()


def gkern_interp(shape,wcs,rs,bprof,fwhm_guess,nsigma=20.0):
    """
    @ brief Returns a normalized 2D kernel array for convolutions
    given a 1D profile shape. 
    rs in radians
    bprof is profile
    fwhm_guess is in arcmin
    """

    fwhm_guess *= np.pi/(180.*60.)
    # Approximate pixel size
    py,px = enmap.pixshape(shape, wcs, signed=False)
    sigma = fwhm_guess/(np.sqrt(8.*np.log(2.)))

    modrmap = enmap.modrmap(shape,wcs)

    ny,nx = shape

    sy = int(nsigma*sigma/py)
    sx = int(nsigma*sigma/px)
    
    if ((ny%2==0) and (sy%2==1)) or ((ny%2==1) and (sy%2==0)): sy+=1
    if ((nx%2==0) and (sx%2==1)) or ((nx%2==1) and (sx%2==0)): sx+=1
    
    
    rmap = crop_center(modrmap,sy,sx)

    g = interp(rs,bprof)(rmap)

    return g / g.sum()


def convolve_profile(imap,rs,bprof,fwhm_guess,nsigma=20.0):
    """
    rs in radians
    bprof is profile
    fwhm_guess is in arcmin
    """
    g = gkern_interp(imap.shape,imap.wcs,rs,bprof,fwhm_guess,nsigma=nsigma)
    print(g.shape)
    return convolve(imap,g)

def convolve(imap,kernel):
    from scipy import signal

    g = kernel
    ncomps = imap.shape[0] if imap.ndim>2 else 1
    imaps = imap.reshape((ncomps,imap.shape[-2],imap.shape[-1]))
    data = []
    for i in range(imaps.shape[0]):
        omap = signal.convolve(imaps[i],g, mode='same')
        data.append(omap)

    if ncomps==1:
        data = np.array(data).reshape((imap.shape[-2],imap.shape[-1]))
    else:
        data = np.array(data).reshape((ncomps,imap.shape[-2],imap.shape[-1]))
    
    return enmap.enmap(data,imap.wcs)


def convolve_gaussian(imap,fwhm=None,nsigma=5.0):
    """
    @brief convolve a map with a Gaussian beam (real space operation)
    @param kernel real-space 2D kernel
    @param fwhm Full Width Half Max in arcmin
    @param nsigma Number of sigmas the Gaussian kernel is defined out to.

    @param sigmaY standard deviation of Gaussian in pixel units in the Y direction
    @param sigmaX standard deviation of Gaussian in pixel units in the X direction

    """

    fwhm *= np.pi/(180.*60.)
    py,px = enmap.pixshape(imap.shape, imap.wcs)
    sigmaY = fwhm/(np.sqrt(8.*np.log(2.))*py)
    sigmaX = fwhm/(np.sqrt(8.*np.log(2.))*px)

    g = gauss_kern(sigmaY, sigmaX,nsigma=nsigma)
        
    return convolve(imap,g)


def get_grf_cmb(shape,wcs,theory,spec,seed=None):
    modlmap = enmap.modlmap(shape,wcs)
    lmax = modlmap.max()
    ells = np.arange(0,lmax,1)
    Ny,Nx = shape[-2:]
    return get_grf_realization(shape,wcs,interp(ells,theory.gCl(spec,ells))(modlmap).reshape((1,1,Ny,Nx)),seed=None)
    
        
def get_grf_realization(shape,wcs,power2d,seed=None):
    mg = MapGen(shape,wcs,power2d)
    return mg.get_map(seed=seed)


class QuickSim(object):
    def __init__(self,deg,px,theory=None,pol=False):
        from orphics import cosmology
        self.shape,self.wcs = rect_geometry(width_deg=deg,px_res_arcmin=px)
        self.modlmap = enmap.modlmap(self.shape,self.wcs)
        self.lmax = self.modlmap.max()
        self.ells = np.arange(0,self.lmax,1)
        if theory is None: theory = cosmology.default_theory()
        self.theory = theory
        ncomp = 3 if pol else 1
        ps = np.zeros((ncomp,ncomp,self.ells.size))
        self.cltt = theory.lCl('TT',self.ells)
        ps[0,0] = self.cltt
        if pol:
            self.clee = theory.lCl('EE',self.ells)
            self.clte = theory.lCl('TE',self.ells)
            self.clbb = theory.lCl('BB',self.ells)
            ps[1,1] = self.clee
            ps[2,2] = self.clbb
            ps[0,1] = self.clte
            ps[1,0] = self.clte
        if pol: self.shape = (3,)+self.shape
        self.mgen = MapGen(self.shape,self.wcs,ps)
        self.fc = FourierCalc(self.shape,self.wcs)
    def get_map(self,seed=None):
        return self.mgen.get_map(seed=seed)
            
def ftrans(p2d,tfunc=np.log10):
    wcs = None
    try: wcs = p2d.wcs
    except: pass
    t2d = tfunc(np.fft.fftshift(p2d))
    if wcs is None:
        return t2d
    else:
        return enmap.enmap(t2d,wcs)

def real_space_filter(kfilter):
    return np.fft.ifftshift(ifft(kfilter+0j,normalize=True,axes=[-2,-1]).real)

def rfilter(imap,kfilter=None,rfilter=None,mode='same',boundary='wrap',**kwargs):
    """
    Filter a real-space map imap with a k-space filter kfilter
    but using a real-space convolution.
    """
    if rfilter is None: rfilter = real_space_filter(kfilter)
    from scipy.signal import convolve2d
    return enmap.samewcs(convolve2d(imap,rfilter,mode=mode,boundary=boundary,**kwargs),imap)
    

def rgeo(degrees,pixarcmin,**kwargs):
    """
    Return shape,wcs geometry pair for patch of width degrees and 
    resolution pixarcmin.
    """
    return rect_geometry(width_deg=degrees,px_res_arcmin=pixarcmin,**kwargs)



class SymMat(object):
    """
    A memory efficient but not very flexible symmetric matrix.
    If a matrix (e.g. covariance) is large but symmetric,
    this lets you reduce the memory footprint by <50% by
    only storing the upper right triangle.
    
    e.g.:
    >>> a = SymMat(3,(100,100))
    >>> a[0,1] = np.ones((100,100))
    After this, a[0,1] and and a[1,0] will return the same
    matrix.
    However, only two leading indices are supported (hence, a matrix)
    and the usual numpy slicing on these doesn't work. a[0][1] doesn't
    work either. The trailing dimensions can be of arbitary shape.
    e.g.
    >>> a = SymMat(3,(2,100,100))
    is also valid.


    You can convert the symmetric matrix to a full footprint good old
    numpy array with:
    >>> array = a.to_array()

    However, you usually don't want to do this on the full array, since
    the whole point of using this was to never have the full matrix
    in memory. Instead, you are allowed to specify a slice of the
    trailing dimensions:
    >>> array = a.to_array(np.s_[:10,:10])
    allowing you to loop over slices as you please.

    """
    def __init__(self,ncomp,shape,data=None):
        self.ncomp = ncomp
        self.shape = shape
        ndat = ncomp*(ncomp+1)//2
        self.data = data if data is not None else np.empty((ndat,)+shape)
        
    def yx_to_k(self,y,x):
        if y>x: return self.yx_to_k(x,y)
        return y*self.ncomp+x - y*(y+1)//2
    
    def __getitem__(self, tup):
        y, x = tup
        return self.data[self.yx_to_k(y,x)]
    
    def __setitem__(self, tup, data):
        y, x = tup
        self.data[self.yx_to_k(y,x)] = data
        
    def to_array(self,sel=np.s_[...],flatten=False):
        """
        Convert the SymMat object to a numpy array, optionally selecting a 
        slice of the data.

        Args:
            sel: a numpy slice allowing for selection of the projected array.
            Use np.s_ to construct this.
            flatten: whether to flatten the array before selecting with sel 
        """
        oshape = self.data[0].reshape(-1)[sel].shape if flatten else self.data[0][sel].shape
        out = np.empty((self.ncomp,self.ncomp,)+oshape)
        for y in range(self.ncomp):
            for x in range(y,self.ncomp):
                kindex = self.yx_to_k(y,x)
                data = self.data[kindex].reshape(-1) if flatten else self.data[kindex]
                out[y,x] = data[sel].copy()
                if x!=y: out[x,y] = out[y,x].copy()
        return out

def symmat_from_data(data):
    ndat = data.shape[0]
    shape = data.shape[1:]
    ncomp = int(0.5*(np.sqrt(8*ndat+1)-1))
    return SymMat(ncomp,shape,data=data)



class SplitSimulator(object):
    def __init__(self,shape,wcs,beams,freqs,noises,nsplits,
                 lknees=None,alphas=None,pss=None,nu0=None,
                 lmins=None,lmaxs=None,
                 theory=None,atmosphere=True,lensing=False,
                 dust=False,do_fgs=False,
                 lpass=False,aseed=1,ell_min_noise=200):
        self.lensing = lensing
        self.lpass = lpass
        self.aseed = aseed
        if pss is not None:
            ncomp = pss.shape[0]
        else:
            if lensing: ncomp = 1
            assert not(do_fgs)
            assert not(dust)
        self.fgs = do_fgs
        self.dust = dust
        if not(atmosphere) or (lknees is None) or (alphas is None) :
            lknees = [0.]*len(freqs)
            alphas = [1.]*len(freqs)
            warnings.warn("No atmosphere in split simulator.")
        self.nu0 = nu0
        self.freqs = freqs
        self.lmins = lmins
        self.lmaxs = lmaxs
        self.modlmap = enmap.modlmap(shape,wcs)
        lmax = self.modlmap.max()
        if theory is None: theory = cosmology.default_theory()
        ells = np.arange(0,lmax,1)
        cltt = theory.uCl('TT',ells) if lensing else theory.lCl('TT',ells)
        self.theory = theory
        self.cseed = 0
        self.kseed = 1
        self.nseed = 2
        if (pss is None) and lensing:
            pss = theory.gCl('kk',ells)[None,None]
        if pss is not None:
            self.fgen = MapGen((ncomp,)+shape[-2:],wcs,pss)
        self.cgen = MapGen(shape[-2:],wcs,cltt[None,None])
        self.shape, self.wcs = shape,wcs
        self.arrays = range(len(freqs))
        self.nsplits = np.asarray(nsplits).astype(np.int)
        self.ngens = []
        self.kbeams = []
        self.ebeams = []
        self.ps_noises = []
        self.eps_noises = []
        for array in self.arrays:
            ps_noise = cosmology.noise_func(ells,0.,noises[array],lknee=lknees[array],alpha=alphas[array])
            ps_noise[ells<ell_min_noise] = ps_noise[ells>=ell_min_noise].max()
            if lpass: ps_noise[ells<lmins[array]] = 0
            self.ps_noises.append(interp(ells,ps_noise.copy())(self.modlmap))
            self.eps_noises.append(ps_noise.copy())
            self.ngens.append( MapGen(shape[-2:],wcs,ps_noise[None,None]*nsplits[array]) )
            self.kbeams.append( gauss_beam(self.modlmap,beams[array]) )
            self.ebeams.append( gauss_beam(ells,beams[array]))
        self.ells = ells

    def get_corr(self,seed):
        fmap = self.fgen.get_map(seed=(self.aseed,self.kseed,seed),scalar=True)
        return fmap

    def _lens(self,unlensed,kappa,lens_order=5):
        from orphics import lensing
        from pixell import lensing as enlensing
        self.kappa = kappa
        alpha = lensing.alpha_from_kappa(kappa,posmap=enmap.posmap(self.shape,self.wcs))
        lensed = enlensing.displace_map(unlensed, alpha, order=lens_order)
        return lensed

    def get_sim(self,seed):
        from szar import foregrounds as fg
        if self.lensing or self.fgs:
            ret = self.get_corr(seed)
            if self.dust:
                kappa,tsz,cib = ret
            elif self.fgs:
                kappa,tsz = ret
            else:
                kappa = ret[0]
        
        unlensed = self.cgen.get_map(seed=(self.aseed,self.cseed,seed))
        lensed = self._lens(unlensed,kappa) if self.lensing else unlensed
        self.lensed = lensed.copy()
        tcmb = 2.726e6
        if self.fgs: self.y = tsz.copy()/tcmb/fg.ffunc(self.nu0)
        observed = []
        noises = []
        for array in self.arrays:
            if self.fgs:
                scaled_tsz = tsz * fg.ffunc(self.freqs[array]) / fg.ffunc(self.nu0) if self.fgs else 0.
            else:
                scaled_tsz = 0.
            if self.dust:
                scaled_cib = cib * fg.cib_nu(self.freqs[array]) / fg.cib_nu(self.nu0) if self.fgs else 0.
            else:
                scaled_cib = 0.
            sky = lensed + scaled_tsz + scaled_cib
            beamed = filter_map(sky,self.kbeams[array])
            observed.append([])
            noises.append([])
            for split in range(self.nsplits[array]):
                noise = self.ngens[array].get_map(seed=(self.aseed,self.nseed,seed,split,array))
                observed[array].append(beamed+noise)
                noises[array].append(noise)
            observed[array] = enmap.enmap(np.stack(observed[array]),self.wcs)
            noises[array] = enmap.enmap(np.stack(noises[array]),self.wcs)
            if self.lpass:
                observed[array] = filter_map(observed[array],mask_kspace(self.shape,self.wcs,lmin=self.lmins[array]))             
                noises[array] = filter_map(noises[array],mask_kspace(self.shape,self.wcs,lmin=self.lmins[array]))             
        return observed,noises


class PolLensSplit(object):
    def __init__(self,shape,wcs,beam,noise,nsplits,
                 theory=None,
                 aseed=1):
        self.aseed = aseed
        self.modlmap = enmap.modlmap(shape,wcs)
        lmax = self.modlmap.max()
        if theory is None: theory = cosmology.default_theory()
        ells = np.arange(0,lmax,1)
        cmb_ps = cosmology.enmap_power_from_orphics_theory(theory,ells=ells,lensed=False,dimensionless=False,orphics_dimensionless=False)
        self.theory = theory
        self.cseed = 0
        self.kseed = 1
        self.nseed = 2
        pss = theory.gCl('kk',ells)[None,None]
        self.kgen = MapGen((1,)+shape[-2:],wcs,pss)
        self.cgen = MapGen((3,)+shape[-2:],wcs,cmb_ps)
        self.shape, self.wcs = shape,wcs
        self.nsplits = nsplits
        tnoise = cosmology.noise_func(self.modlmap,0.,noise)
        pnoise = cosmology.noise_func(self.modlmap,0.,noise) * 2.
        self.ps_noise = enmap.zeros((3,3,)+self.shape[-2:])
        self.ps_noise[0,0] = tnoise.copy()
        self.ps_noise[1,1] = pnoise.copy()
        self.ps_noise[2,2] = pnoise.copy()
        self.ngen = MapGen((3,)+shape[-2:],wcs,self.ps_noise*nsplits)
        self.kbeam =  gauss_beam(self.modlmap,beam)

    def get_kappa(self,seed):
        kmap = self.kgen.get_map(seed=(self.aseed,self.kseed,seed),scalar=True)
        return kmap

    def _lens(self,unlensed,kappa,lens_order=5):
        from orphics import lensing
        from pixell import lensing as enlensing
        self.kappa = kappa
        alpha = lensing.alpha_from_kappa(kappa,posmap=enmap.posmap(self.shape,self.wcs))
        lensed = enlensing.displace_map(unlensed, alpha, order=lens_order)
        return lensed

    def get_sim(self,seed):
        kappa = self.get_kappa(seed)[0]
        unlensed = self.cgen.get_map(seed=(self.aseed,self.cseed,seed))
        lensed = self._lens(unlensed,kappa) 
        self.lensed = lensed.copy()
        beamed = filter_map(lensed,self.kbeam)
        observed = []
        for split in range(self.nsplits):
            noise = self.ngen.get_map(seed=(self.aseed,self.nseed,seed,split))
            observed.append( beamed+noise )
        observed = enmap.enmap(np.stack(observed),self.wcs)
        return observed

def change_alm_lmax(alms, lmax):
    ilmax  = hp.Alm.getlmax(alms.shape[-1])
    olmax  = lmax

    oshape     = list(alms.shape)
    oshape[-1] = hp.Alm.getsize(olmax)
    oshape     = tuple(oshape)

    alms_out   = np.zeros(oshape, dtype = np.complex128)
    flmax      = min(ilmax, olmax)

    for m in range(flmax+1):
        lminc = m
        lmaxc = flmax

        idx_isidx = hp.Alm.getidx(ilmax, lminc, m)
        idx_ieidx = hp.Alm.getidx(ilmax, lmaxc, m)
        idx_osidx = hp.Alm.getidx(olmax, lminc, m)
        idx_oeidx = hp.Alm.getidx(olmax, lmaxc, m)

        alms_out[..., idx_osidx:idx_oeidx+1] = alms[..., idx_isidx:idx_ieidx+1].copy()


    return alms_out





