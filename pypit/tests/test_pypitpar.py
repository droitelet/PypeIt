# Module to run tests on PypitPar classes
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os

import pytest

from pypit.par import pypitpar
from pypit.par.util import parse_pypit_file
from pypit.spectrographs.util import load_spectrograph

def data_path(filename):
    data_dir = os.path.join(os.path.dirname(__file__), 'files')
    return os.path.join(data_dir, filename)

def test_framegroup():
    pypitpar.FrameGroupPar()

def test_framegroup_types():
    t = pypitpar.FrameGroupPar.valid_frame_types()
    assert 'bias' in t, 'Expected to find \'bias\' in list of valid frame types'

def test_processimages():
    pypitpar.ProcessImagesPar()

def test_flatfield():
    pypitpar.FlatFieldPar()

def test_flexure():
    pypitpar.FlexurePar()

def test_fluxcalibration():
    pypitpar.FluxCalibrationPar()

def test_skysubtraction():
    pypitpar.SkySubtractionPar()

def test_manualextraction():
    pypitpar.ManualExtractionPar()

def test_spectrographs():
    s = pypitpar.ReducePar.valid_spectrographs()
    assert 'keck_lris_blue' in s, 'Expected to find keck_lris_blue as a spectrograph!'

def test_reduce():
    pypitpar.ReducePar()

def test_wavelengthsolution():
    pypitpar.WavelengthSolutionPar()

def test_traceslits():
    pypitpar.TraceSlitsPar()

def test_wavetilts():
    pypitpar.WaveTiltsPar()

def test_traceobjects():
    pypitpar.TraceObjectsPar()

def test_extractobjects():
    pypitpar.ExtractObjectsPar()

def test_calibrations():
    pypitpar.CalibrationsPar()

def test_pypit():
    pypitpar.PypitPar()

def test_fromcfgfile():
    pypitpar.PypitPar.from_cfg_file()

def test_writecfg():
    default_file = 'default.cfg'
    if os.path.isfile(default_file):
        os.remove(default_file)
    pypitpar.PypitPar.from_cfg_file().to_config(cfg_file=default_file)
    assert os.path.isfile(default_file), 'No file written!'
    os.remove(default_file)

def test_readcfg():
    default_file = 'default.cfg'
    if not os.path.isfile(default_file):
        pypitpar.PypitPar.from_cfg_file().to_config(cfg_file=default_file)
    pypitpar.PypitPar.from_cfg_file('default.cfg')
    os.remove(default_file)

def test_mergecfg():
    # Create a file with the defaults
    user_file = 'user_adjust.cfg'
    if os.path.isfile(user_file):
        os.remove(user_file)
    p = pypitpar.PypitPar.from_cfg_file()

    # Make some modifications
    p['rdx']['spectrograph'] = 'keck_lris_blue'
    p['rdx']['pipeline'] = 'ARMS'
    p['calibrations']['biasframe']['useframe'] = 'overscan'

    # Write the modified config
    p.to_config(cfg_file=user_file)
    assert os.path.isfile(user_file), 'User file was not written!'

    # Read it in as a config to merge with the defaults
    p = pypitpar.PypitPar.from_cfg_file(merge_with=user_file)

    # Check the values are correctly read in
    assert p['rdx']['spectrograph'] == 'keck_lris_blue', 'Test spectrograph is incorrect!'
    assert p['rdx']['pipeline'] == 'ARMS', 'Test pipeline is incorrect!'
    assert p['calibrations']['biasframe']['useframe'] == 'overscan', \
                'Test biasframe:useframe is incorrect!'

    # Clean-up
    os.remove(user_file)

def test_sync():
    p = pypitpar.PypitPar()
    proc = pypitpar.ProcessImagesPar()
    proc['combine'] = 'mean'
    proc['sigrej'] = 20.5
    p.sync_processing(proc)
    assert p['scienceframe']['process']['combine'] == 'mean'
    assert p['calibrations']['biasframe']['process']['combine'] == 'mean'
    # Sigma rejection of cosmic rays for arc frames is already turned
    # off by default
    assert p['calibrations']['arcframe']['process']['sigrej'] < 0
    assert p['calibrations']['traceframe']['process']['sigrej'] == 20.5

def test_pypit_file():
    # Read the PypeIt file
    cfg, data, frametype, setups = parse_pypit_file(data_path('example_pypit_file.pypit'),
                                                    file_check=False)
    # Long-winded way of getting the spectrograph name
    name = pypitpar.PypitPar.from_cfg_lines(merge_with=cfg)['rdx']['spectrograph']
    # Instantiate the spectrograph
    spectrograph = load_spectrograph(name)
    # Get the spectrograph specific configuration
    spec_cfg = spectrograph.default_pypit_par().to_config()
    # Initialize the PypeIt parameters merge in the user config
    p = pypitpar.PypitPar.from_cfg_lines(cfg_lines=spec_cfg, merge_with=cfg)
    # Test everything was merged correctly
    # This is a PypitPar default that's not changed
    assert p['calibrations']['pinholeframe']['number'] == 0
    # These are spectrograph specific defaults
    assert p['rdx']['pipeline'] == 'ARMS'
    assert p['fluxcalib'] is not None
    # These are user-level changes
    assert p['calibrations']['arcframe']['number'] == 1
    assert p['calibrations']['biasframe']['process']['sig_lohi'] == [10, 10]
    assert p['calibrations']['traceframe']['process']['combine'] == 'mean'
    assert p['scienceframe']['process']['n_lohi'] == [8, 8]

def test_detector():
    pypitpar.DetectorPar()

def test_telescope():
    pypitpar.TelescopePar()

