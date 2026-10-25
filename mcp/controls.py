"""Fixed runtime mappings from Loxone Structure File v17 (pages 54–60, 72–78, 132)."""
import json
import re

VENT = {'presenceMin': ('setPresenceMin', 'hasPresenceMin'),
        'presenceMax': ('setPresenceMax', 'hasPresenceMax'),
        'absenceMin': ('setAbsenceMin', 'hasAbsenceMin'),
        'absenceMax': ('setAbsenceMax', 'hasAbsenceMax')}

def controls(structure):
    result = dict(structure['controls'])
    for parent_id, parent in structure['controls'].items():
        if parent.get('type') != 'LightControllerV2':
            continue
        for key, child in parent.get('subControls', {}).items():
            if key != child.get('uuidAction') or not key.startswith(parent_id + '/'):
                continue
            if child.get('type') not in ('Switch', 'Dimmer', 'ColorPickerV2'):
                continue
            result[key] = {**child, 'room':parent.get('room'), 'cat':parent.get('cat'),
                           '_parent':parent_id, '_parent_control':parent,
                           'name':parent.get('name', '') + ' / ' + child.get('name', '')}
    return result

def mappings(device):
    typ = device.get('type')
    if typ == 'Jalousie':
        result = {k:dict(command=cmd,state=k,min=0,max=100,unit='%',scale=100)
                  for k,cmd in [('targetPosition','manualPosition'),('targetPositionLamelle','manualLamelle')]
                  if k in device.get('states', {})}
        if 'autoAllowed' in device.get('states', {}):
            result['autoAllowed']=dict(command='setAutomatic',state='autoAllowed',min=0,max=1,unit='boolean',integer=True)
        return result
    if typ == 'Ventilation':
        return {k:dict(command=v[0], state=k, min=0, max=100, unit='%', flag=v[1]) for k,v in VENT.items() if k in device.get('states', {})}
    if typ == 'IRoomControllerV2':
        return {k:dict(command=cmd,state=k,min=10,max=30,unit='temperature',bits=bits)
                for k,cmd,bits in [('comfortTemperature','setComfortTemperature',(1,3)),
                                   ('comfortTemperatureCool','setComfortTemperatureCool',(2,3))]
                if k in device.get('states', {})}
    # Switches are writable only when proven to be light subcontrols.
    if not device.get('_parent'):
        return {}
    if typ == 'Switch':
        return {'active':dict(command='setActive',state='active',min=0,max=1,unit='boolean',integer=True)}
    if typ == 'Dimmer':
        return {'position':dict(command='setPosition',state='position',min=0,max=100,unit='%')}
    if typ == 'ColorPickerV2':
        return {'brightness':dict(command='setColorBrightness',state='color',min=0,max=100,unit='%')}
    return {}

def logic_connected(device, spec):
    if spec.get('flag'):
        return bool(device.get('details', {}).get(spec['flag']))
    bits = device.get('details', {}).get('connectedInputs', 0)
    # Conservatively reject both bit numbering conventions for locked temperatures.
    return any(bits & ((1 << bit) | (1 << (bit-1))) for bit in spec.get('bits', ()))

def color_parts(value):
    if isinstance(value, dict):
        value = value.get('text')
    if isinstance(value, str) and value.startswith('{'):
        value = json.loads(value).get('text')
    match = re.fullmatch(r'(hsv|temp)\((\d+(?:\.\d+)?),(\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?\)', str(value))
    if not match or (match[1]=='hsv') != (match[4] is not None):
        raise ValueError('unsupported_color_state')
    return match[1], [float(x) for x in match.groups()[1:] if x is not None]

def parameter_value(parameter, value):
    if parameter in ('targetPosition','targetPositionLamelle'):
        return float(value)*100
    if parameter != 'brightness':
        return value
    kind, values = color_parts(value)
    return values[2 if kind=='hsv' else 0]

def wire_command(spec, value, raw=None):
    command = spec['command']
    if command == 'setAutomatic':
        return ('auto' if value else 'NoAuto'), None
    if command == 'setActive':
        return ('on' if value else 'off'), None
    if command == 'setPosition':
        return f'{value:g}', None
    if command == 'setColorBrightness':
        kind, values = color_parts(raw)
        values[2 if kind=='hsv' else 0] = value
        return kind+'('+','.join(f'{x:g}' for x in values)+')', None
    return command, value
