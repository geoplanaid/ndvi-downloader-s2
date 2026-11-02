"""Read Sentinel-2 bands onto a fixed per-area grid and write GeoTIFFs.

Pixels are moved with ReadRaster/WriteRaster + numpy.frombuffer instead of
gdal_array, which breaks when another NumPy major version shadows the one
GDAL was built against (common with a user site-packages on Windows).
"""

import json
import math
from dataclasses import dataclass

import numpy as np
from osgeo import gdal, ogr, osr

NODATA = -9999.0
DEFAULT_VALID_SCL = (4, 5, 6, 7)  # vegetation, not vegetated, water, unclassified

GDAL_HTTP_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_TIMEOUT": "60",
    "VSI_CACHE": "TRUE",
}


def configure_gdal_thread():
    """Apply HTTP tuning for the calling thread only, leaving QGIS' global GDAL setup alone."""
    for key, value in GDAL_HTTP_OPTIONS.items():
        gdal.SetThreadLocalConfigOption(key, value)


def srs_from_epsg(epsg):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(epsg))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def utm_epsg(lon, lat):
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return (32600 if lat >= 0 else 32700) + zone


@dataclass
class Grid:
    epsg: int
    minx: float
    miny: float
    maxx: float
    maxy: float
    resolution: float

    @property
    def width(self):
        return int(round((self.maxx - self.minx) / self.resolution))

    @property
    def height(self):
        return int(round((self.maxy - self.miny) / self.resolution))

    @property
    def bounds(self):
        return (self.minx, self.miny, self.maxx, self.maxy)

    @property
    def geotransform(self):
        return (self.minx, self.resolution, 0.0, self.maxy, 0.0, -self.resolution)


@dataclass
class AreaGrid:
    grid: Grid
    geometry: ogr.Geometry  # area polygon in grid CRS
    area_ha: float
    mask: np.ndarray = None  # True inside the polygon


def build_area_grid(aoi_geojson, resolution=10.0):
    """Project an EPSG:4326 polygon to its local UTM zone and snap a pixel grid around it."""
    geom = ogr.CreateGeometryFromJson(json.dumps(aoi_geojson))
    if geom is None or geom.IsEmpty():
        raise ValueError("Geometri area tidak valid")
    centroid = geom.Centroid()
    epsg = utm_epsg(centroid.GetX(), centroid.GetY())
    geom.AssignSpatialReference(srs_from_epsg(4326))
    geom.TransformTo(srs_from_epsg(epsg))
    minx, maxx, miny, maxy = geom.GetEnvelope()
    r = float(resolution)
    grid = Grid(
        epsg=epsg,
        minx=math.floor(minx / r) * r,
        miny=math.floor(miny / r) * r,
        maxx=max(math.ceil(maxx / r) * r, math.floor(minx / r) * r + r),
        maxy=max(math.ceil(maxy / r) * r, math.floor(miny / r) * r + r),
        resolution=r,
    )
    return AreaGrid(grid=grid, geometry=geom, area_ha=geom.GetArea() / 10_000)


def area_hectares(aoi_geojson):
    """Area of an EPSG:4326 polygon in hectares, measured in its UTM zone."""
    return build_area_grid(aoi_geojson, resolution=10.0).area_ha


def _read_band(ds, gdal_type, np_type):
    band = ds.GetRasterBand(1)
    raw = band.ReadRaster(0, 0, ds.RasterXSize, ds.RasterYSize, buf_type=gdal_type)
    return np.frombuffer(raw, dtype=np_type).reshape(ds.RasterYSize, ds.RasterXSize)


def polygon_mask(area):
    """Rasterize the area polygon on its grid (ALL_TOUCHED for very small areas)."""
    g = area.grid
    mem = gdal.GetDriverByName("MEM").Create("", g.width, g.height, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(g.geotransform)
    mem.SetProjection(srs_from_epsg(g.epsg).ExportToWkt())
    vds = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("")
    layer = vds.CreateLayer("area", srs=srs_from_epsg(g.epsg))
    feat = ogr.Feature(layer.GetLayerDefn())
    feat.SetGeometry(area.geometry)
    layer.CreateFeature(feat)
    gdal.RasterizeLayer(mem, [1], layer, burn_values=[1])
    mask = _read_band(mem, gdal.GDT_Byte, np.uint8).astype(bool)
    if not mask.any():
        gdal.RasterizeLayer(mem, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        mask = _read_band(mem, gdal.GDT_Byte, np.uint8).astype(bool)
    return mask


def _vsicurl(href):
    return href if href.startswith("/vsi") else f"/vsicurl/{href}"


def _warp_to_grid(href, grid, resample):
    ds = gdal.Warp(
        "",
        _vsicurl(href),
        options=gdal.WarpOptions(
            format="MEM",
            outputBounds=grid.bounds,
            width=grid.width,
            height=grid.height,
            dstSRS=f"EPSG:{grid.epsg}",
            resampleAlg=resample,
            srcNodata=0,
            dstNodata=0,
        ),
    )
    if ds is None:
        raise IOError(f"Gagal membaca {href}: {gdal.GetLastErrorMsg()}")
    return _read_band(ds, gdal.GDT_UInt16, np.uint16)


def effective_offset(item, red_dn, nir_dn):
    """Double-check the metadata offset against the pixels themselves.

    With the +1000 DN offset present, values below ~1000 would need negative
    reflectance, so if a few percent of pixels sit clearly below it the offset
    has already been removed (seen on Earth Search items flagged
    ``boa_offset_applied: false``). Applying it anyway drives NDVI to 1.
    """
    if item.offset == 0 or red_dn.size == 0:
        return item.offset
    dn = np.concatenate([red_dn, nir_dn])
    if np.percentile(dn, 2) < 900:
        return 0.0
    return item.offset


def scene_ndvi_on_grid(item, area, valid_scl=DEFAULT_VALID_SCL):
    """NDVI of one scene on the area grid; NaN where cloudy, outside the scene or outside the polygon.

    Returns None when the scene has no data inside the area.
    """
    g = area.grid
    same_grid = g.resolution == 10 and g.epsg == item.epsg
    band_resample = "near" if same_grid else ("average" if g.resolution > 10 else "bilinear")
    scl_resample = "near" if g.resolution <= 20 else "mode"

    red = _warp_to_grid(item.red, g, band_resample)
    has_data = area.mask & (red > 0)
    if not has_data.any():
        return None
    nir = _warp_to_grid(item.nir, g, band_resample)
    scl = _warp_to_grid(item.scl, g, scl_resample)
    has_data &= nir > 0
    valid = has_data & np.isin(scl, valid_scl)

    offset = effective_offset(item, red[has_data], nir[has_data])
    out = np.full(red.shape, np.nan, dtype=np.float32)
    r = red[valid].astype(np.float32) * item.scale + offset
    n = nir[valid].astype(np.float32) * item.scale + offset
    denom = n + r
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = np.where(denom > 1e-4, (n - r) / denom, np.nan)
    out[valid] = np.clip(ndvi, -1.0, 1.0)
    return out


def write_geotiff(path, array, grid, description=None, metadata=None):
    """Write a float32 single-band GeoTIFF; NaN becomes NODATA."""
    data = np.where(np.isnan(array), NODATA, array).astype(np.float32)
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        path, grid.width, grid.height, 1, gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"],
    )
    if ds is None:
        raise IOError(f"Gagal membuat {path}: {gdal.GetLastErrorMsg()}")
    ds.SetGeoTransform(grid.geotransform)
    ds.SetProjection(srs_from_epsg(grid.epsg).ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteRaster(0, 0, grid.width, grid.height, np.ascontiguousarray(data).tobytes(),
                     buf_type=gdal.GDT_Float32)
    if description:
        band.SetDescription(description)
    if metadata:
        ds.SetMetadata({k: str(v) for k, v in metadata.items()})
    band.FlushCache()
    ds = None
