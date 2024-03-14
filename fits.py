
FITS_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

# standard header key/value pairs
FITS_STANDARD_FIELDS = {
    'NAXIS':        '2',
    'TELESCOPE':    'WAO-MAST',
    'INSTRUMENT':   'DEEPSPEC',
    'DETECTOR':     'GE 1024 1024 BI DD',
}


FITS_HEADER_COMMENTS = {
    'TELESCOPE':        'TELESCOPE NAME',
    'INSTRUMENT':       'INSTRUMENT NAME',
    'DETECTOR':         'DETECTOR MODEL',
    'BAND':             'DEEPSPEC BAND',
    'CAMERA_IP':        'CAMERA IP',
    'TYPE':             'EXPOSURE TYPE',
    'LOCAL_T_START':    'EXPOSURE START TIME [local]',
    'LOCAL_T_MID':      'EXPOSURE MID TIME [local]',
    'LOCAL_T_END':      'EXPOSURE END TIME [local]',
    'T_START':          'EXPOSURE START TIME [UTC]',
    'T_MID':            'EXPOSURE MID TIME [UTC]',
    'T_END':            'EXPOSURE END TIME [UTC]',
    'T_EXP':            'TOTAL INTEGRATION TIME',
    'TEMP_GOAL':        'GOAL DETECTOR TEMPERATURE',
    'TEMP_SAFE_FLAG':   'DETECTOR BACKSIDE TEMPERATURE SAFETY FLAG',
    'DATE-OBS':         'OBSERVATION DATE',
    'MJD-OBS':          'MJD OF OBSERVATION MIDPOINT',
    'READOUT_SPEED':    'PIXEL READOUT FREQUENCY',
    'CDELT1':           'BINNING IN THE X DIRECTION',
    'CDELT2':           'BINNING IN THE Y DIRECTION',
    'NAXIS':            'NUMBER OF AXES IN FRAME',
    'NAXIS1':           'NUMBER OF PIXELS IN THE X DIRECTION',
    'NAXIS2':           'NUMBER OF PIXELS IN THE Y DIRECTION',
    'PIXEL_SIZE':       'PIXEL SIZE IN MICRONS',
    'BITPIX':           '# of bits storing pix values',
}