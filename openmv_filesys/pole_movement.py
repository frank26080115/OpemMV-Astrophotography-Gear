import micropython
micropython.opt_level(2)

import math
import comutils
from comutils import PIXELS_PER_DEGREE

POLARIS_2000 = micropython.const([2, 31, 51.56, 89, 15, 51.5]) # obtained from Stellarium
POLARIS_2020 = micropython.const([2, 57, 39.08, 89, 20, 57.4]) # obtained from Stellarium

"""
The Earth's axis is very slowly wobbling
so the position of any star is very slowly moving
If you purchase a polar-scope, inside you will see that there's a ring for each year, and you line up Polaris with the ring corresponding to the current year

The code below will predict the new coordinate of Polaris according to the current date
It uses simple interpolation and transforms between coordinate systems
It accounts for leap years correctly
Testing of the code below shows that for the year 2024, the declination (the important part) is still accurate down to the arc second, and RA (not as important) is off by 0.8% (12 arc minutes)
"""

class PoleMovement(object):
    def __init__(self):
        ra2000, dec2000 = conv_ra_dec(POLARIS_2000)
        ra2020, dec2020 = conv_ra_dec(POLARIS_2020)
        self.x2000, self.y2000 = vector(ra2000, dec2000)
        self.x2020, self.y2020 = vector(ra2020, dec2020)
        self.jdn2000 = comutils.jdn(2000, 1, 1)
        self.jdn2020 = comutils.jdn(2020, 1, 1)
        dx = self.x2020 - self.x2000
        dy = self.y2020 - self.y2000
        days = self.jdn2020 - self.jdn2000
        self.dxr = dx / days
        self.dyr = dy / days

    def calc_for_date(self, year, month, day):
        jdn_now = comutils.jdn(year, month, day)
        return self.calc_for_jdn(jdn_now)

    def calc_for_jdn(self, x):
        days = x - self.jdn2020
        x = self.x2020 + (self.dxr * days)
        y = self.y2020 + (self.dyr * days)
        return star_coord(x, y)

def conv_ra_dec(x):
    ra  = x[0] + (x[1] / 60.0) + (x[2] / (60.0 * 60.0))
    dec = x[3] + (x[4] / 60.0) + (x[5] / (60.0 * 60.0))
    return ra, dec

def vector(ra, dec):
    rho = (90 - dec) * PIXELS_PER_DEGREE
    phi = math.radians(360.0 * ra / 24.0)
    x = rho * math.cos(phi)
    y = rho * math.sin(phi)
    return x, y

def star_coord(x, y):
    mag, ang = comutils.vector_between([0, 0], [x, y])
    while ang < 0:
        ang += 360.0
    mag /= PIXELS_PER_DEGREE
    mag = 90.0 - mag
    ang /= 360.0
    ang *= 24.0
    return ang, mag

"""
if __name__ == "__main__":
    m = PoleMovement()
    ra, dec = m.calc_for_date(2020, 1, 1)
    print("ra = %f    ,    dec = %f" % (ra, dec))
"""
