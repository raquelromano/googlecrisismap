// Copyright 2012 Google Inc.  All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License.  You may obtain a copy
// of the License at: http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software distrib-
// uted under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
// OR CONDITIONS OF ANY KIND, either express or implied.  See the License for
// specific language governing permissions and limitations under the License.

/**
 * @fileoverview Geometry functions for managing polygons and tile bounds.
 *     TODO(romano): Replace this file since there are known bugs in
 *     quadTileOverlap, and nothing in this file is in the 'cm' namespace
 *     (b/7979024).
 * @author giencke@google.com (Pete Giencke)
 */

goog.provide('cm.geometry');

goog.require('cm');
goog.require('cm.ui');
goog.require('goog.array');

/**
 * @enum {number}
 */
var Overlap = {
  OUTSIDE: 0,
  INSIDE: 1,
  INTERSECTING: 2
};

/**
 * Tests whether the bounding boxes of the quad and the rectangle given by
 * low, high overlap.
 * @param {Array.<google.maps.Point>} quad The bounding box of a quadrangle.
 * @param {google.maps.Point} low The bottom-left of a rectangle.
 * @param {google.maps.Point} high The top-right of the rectangle.
 * @return {boolean} True if the boxes overlap.
 */
function boundingBoxesOverlap(quad, low, high) {
  if (quad[0].x < low.x &&
      quad[1].x < low.x &&
      quad[2].x < low.x &&
      quad[3].x < low.x) {
    return false;
  }
  if (quad[0].y < low.y &&
      quad[1].y < low.y &&
      quad[2].y < low.y &&
      quad[3].y < low.y) {
    return false;
  }
  if (quad[0].x > high.x &&
      quad[1].x > high.x &&
      quad[2].x > high.x &&
      quad[3].x > high.x) {
    return false;
  }
  if (quad[0].y > high.y &&
      quad[1].y > high.y &&
      quad[2].y > high.y &&
      quad[3].y > high.y) {
    return false;
  }
  return true;
}

/**
 * Intersects the line given by a, b with the rectangle given by low, high.
 * It computes dot products with the normal of the line.
 * Returns OUTSIDE, INSIDE, or INTERSECTING, which specifies the line's
 * relationship to the bounding box.
 * @param {google.maps.Point} a First endpoint of the line.
 * @param {google.maps.Point} b Second endpoint of the line.
 * @param {google.maps.Point} low Minimum (x, y) corner of the rectangle.
 * @param {google.maps.Point} high MAximum (x, y) corner of the rectangle.
 * @return {Overlap} The overlap status (INSIDE, OUTSIDE, or INTERSECTING).
 */
function edgeTileOverlap(a, b, low, high) {
  var nx = b.y - a.y;
  var ny = a.x - b.x;
  var d = nx * a.x + ny * a.y;
  var d_lo_lo = nx * low.x + ny * low.y - d;
  var d_lo_hi = nx * low.x + ny * high.y - d;
  var d_hi_lo = nx * high.x + ny * low.y - d;
  var d_hi_hi = nx * high.x + ny * high.y - d;
  if (d_lo_lo < 0 &&
      d_lo_hi < 0 &&
      d_hi_lo < 0 &&
      d_hi_hi < 0) {
    return Overlap.OUTSIDE;
  } else if (d_lo_lo > 0 &&
             d_lo_hi > 0 &&
             d_hi_lo > 0 &&
             d_hi_hi > 0) {
    return Overlap.INSIDE;
  } else {
    return Overlap.INTERSECTING;
  }
}

/**
 * Returns the type of overlap between the quadrilateral and the box defined by
 * the given points. The quadrilateral must be convex and its points must be
 * specified in counter-clockwise order.
 * @param {Array.<google.maps.Point>} quad The quadrilateral with which to
 *     intersect the box.
 * @param {google.maps.Point} upperLeft The upper-left box corner.
 * @param {google.maps.Point} lowerRight The lower-right box corner.
 * @return {Overlap} The type of overlap.
 */
function quadTileOverlap(quad, upperLeft, lowerRight) {
  if (!boundingBoxesOverlap(quad, upperLeft, lowerRight)) {
    return Overlap.OUTSIDE;
  }
  var a = edgeTileOverlap(quad[0], quad[1], upperLeft, lowerRight);
  var b = edgeTileOverlap(quad[1], quad[2], upperLeft, lowerRight);
  var c = edgeTileOverlap(quad[2], quad[3], upperLeft, lowerRight);
  var d = edgeTileOverlap(quad[3], quad[0], upperLeft, lowerRight);
  if (a == Overlap.OUTSIDE || b == Overlap.OUTSIDE || c == Overlap.OUTSIDE ||
      d == Overlap.OUTSIDE) {
    return Overlap.OUTSIDE;
  }
  if (a == Overlap.INSIDE && b == Overlap.INSIDE && c == Overlap.INSIDE &&
      d == Overlap.INSIDE) {
    return Overlap.INSIDE;
  }
  return Overlap.INTERSECTING;
}

/**
 * Applies the projection to the array of lat-lng coordinates.
 * @param {google.maps.Projection} projection The map projection.
 * @param {Array.<google.maps.LatLng>} latlngs An array of lat/lng
 *     coordinates to project.
 * @return {Array.<google.maps.Point>} The projected points.
 */
function applyProjection(projection, latlngs) {
  return goog.array.map(latlngs, function(latlng) {
    return projection.fromLatLngToPoint(latlng);
  });
}

/**
 * Return the world coordinates of the upper-left and lower-right corners of
 * the given tile.
 * @param {number} x The tile's x-coordinate.
 * @param {number} y The tile's y-coordinate.
 * @param {number} zoom The tile's zoom level.
 * @return {Array.<google.maps.Point>} The tile's upper-left and
 *     lower-right coordinates.
 */
function getTileRange(x, y, zoom) {
  var z = Math.pow(2, zoom);

  /**
   * @param {number} dx An x offset in tile coordinates.
   * @param {number} dy A y offset in tile coordinates.
   * @return {google.maps.Point} The point at the given tile coordinates.
   */
  function p(dx, dy) {
    return new google.maps.Point((x + dx) * 256 / z,
                                 (y + dy) * 256 / z);
  }

  return [p(0, 0), p(1, 1)];
}

/**
 * Creates a rough bounding box based upon layer x, y, and z.
 * @param {google.maps.Projection} projection A projection.
 * @param {google.maps.LatLng} latLng The center of the box.
 * @param {number} zoom The zoom level.
 * @return {Array.<google.maps.LatLng>} The four corners of the box.
 */
function getBoundingBox(projection, latLng, zoom) {
  // A magical number which helps make the viewport a bit bigger
  var SCALE_FACTOR = 1.1;
  var w = cm.ui.document.body.offsetWidth;
  var h = cm.ui.document.body.offsetHeight;
  var xy = projection.fromLatLngToPoint(latLng);
  var scale = Math.pow(2, zoom * SCALE_FACTOR);
  var llx = xy.x - w / scale;
  var lly = xy.y + h / scale;
  var urx = xy.x + w / scale;
  var ury = xy.y - h / scale;
  var ll = projection.fromPointToLatLng(new google.maps.Point(llx, lly));
  var ur = projection.fromPointToLatLng(new google.maps.Point(urx, ury));

  var polyCoords = [
      new google.maps.LatLng(ll.lat(), ll.lng()),
      new google.maps.LatLng(ll.lat(), ur.lng()),
      new google.maps.LatLng(ur.lat(), ur.lng()),
      new google.maps.LatLng(ur.lat(), ll.lng()),
      new google.maps.LatLng(ll.lat(), ll.lng())
  ];
  return polyCoords;
}
