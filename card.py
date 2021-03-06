#!/usr/bin/python
# Copyright 2014 Google Inc.  All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy
# of the License at: http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distrib-
# uted under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied.  See the License for
# specific language governing permissions and limitations under the License.

"""Displays a card containing a list of nearby features for a given topic."""

import cgi
import datetime
import json
import logging
import math
import re
import urllib
import urlparse

import base_handler
import cache
import config
import kmlify
import maproot
import model
import utils

from google.appengine.api import urlfetch
from google.appengine.ext import ndb  # just for GeoPt

# A cache of Feature list representing points from XML, keyed by
# [url, map_id, map_version_id, layer_id]
XML_FEATURES_CACHE = cache.Cache('card_features.xml', 300)

# Fetched strings of Google Places API JSON results, keyed by request URL.
JSON_PLACES_API_CACHE = cache.Cache('card.places', 300)

# Lists of Feature objects, keyed by [map_id, map_version_id, topic_id,
# geolocation_rounded_to_10m, radius, max_count].
FILTERED_FEATURES_CACHE = cache.Cache('card.filtered_features', 60)

# Key: [map_id, map_version_id, topic_id, geolocation_rounded_to_10m, radius].
# Value: 3-tuple of (latest_answers, answer_times, report_dicts) where
#   - latest_answers is a dictionary {qid: latest_answer_to_that_question}
#   - answer_times is a dictionary {qid: effective_time_of_latest_answer}
#   - report_dicts contains the last REPORTS_PER_FEATURE reports, as a list
#     of dicts [{qid: answer, '_effective': time, '_id': report_id}]
REPORT_CACHE = cache.Cache('card.reports', 15)

# Number of crowd reports to cache and return per feature.
REPORTS_PER_FEATURE = 5

MAX_ANSWER_AGE = datetime.timedelta(days=7)  # ignore answers older than 7 days
GOOGLE_SPREADSHEET_CSV_URL = (
    'https://docs.google.com/spreadsheet/pub?key=$key&output=csv')
DEGREES = 3.14159265358979/180
DEADLINE = 10
PLACES_API_SEARCH_URL = (
    'https://maps.googleapis.com/maps/api/place/nearbysearch/json?')
PLACES_API_DETAILS_URL = (
    'https://maps.googleapis.com/maps/api/place/details/json?')


def RoundGeoPt(point):
  return '%.4f,%.4f' % (point.lat, point.lon)  # 10-m resolution


class Feature(object):
  """A feature (map item) from a source data layer."""

  def __init__(self, name, description_html, location, layer_id=None,
               layer_type=None, gplace_id=None, html_attrs=None):
    self.name = name
    self.layer_id = layer_id
    self.location = location  # should be an ndb.GeoPt
    self.description_html = description_html
    self.html_attrs = html_attrs or []
    self.layer_type = layer_type
    self.gplace_id = gplace_id  # Google Places place_id
    self.distance = None
    self.status_color = None
    self.answer_text = ''
    self.answer_time = ''
    self.answer_source = ''
    self.answers = {}
    self.reports = []

  def __lt__(self, other):
    return self.distance < other.distance

  def __eq__(self, other):
    return self.__dict__ == other.__dict__

  distance_km = property(lambda self: self.distance and self.distance/1000.0)
  distance_mi = property(lambda self: self.distance and self.distance/1609.344)


def EarthDistance(a, b):
  """Great circle distance in metres between two points on the Earth."""
  lat1, lon1 = a.lat*DEGREES, a.lon*DEGREES
  lat2, lon2 = b.lat*DEGREES, b.lon*DEGREES
  dlon = lon2 - lon1

  atan2, cos, sin, sqrt = math.atan2, math.cos, math.sin, math.sqrt
  y = sqrt(pow(cos(lat2)*sin(dlon), 2) +
           pow(cos(lat1)*sin(lat2) - sin(lat1)*cos(lat2)*cos(dlon), 2))
  x = sin(lat1)*sin(lat2) + cos(lat1)*cos(lat2)*cos(dlon)
  return 6378000*atan2(y, x)


def GetText(element):
  return (element is not None) and element.text or ''


def GetFeaturesFromXml(xml_content, layer_id=None):
  """Extracts a list of Feature objects from KML, GeoRSS, or Atom content."""
  root = kmlify.ParseXml(xml_content)
  for element in root.getiterator():
    element.tag = element.tag.split('}')[-1]  # remove XML namespaces
  features = []
  for item in (root.findall('.//Placemark') +
               root.findall('.//entry') + root.findall('.//item')):
    location = GetLocationFromXmlItem(item)
    if not location:
      continue
    texts = {child.tag: GetText(child) for child in item.getchildren()}
    # For now strip description of all the html tags to prevent XSS
    # vulnerabilities except some basic text formatting tags
    # TODO(user): sanitization should move closer to render time
    # (revisit this once iframed version goes away) - b/17374443
    description_html = (texts.get('description') or
                        texts.get('content') or
                        texts.get('summary') or '')
    description_escaped = utils.StripHtmlTags(
        description_html, tag_whitelist=['b', 'u', 'i', 'br'])
    features.append(Feature(texts.get('title') or texts.get('name'),
                            description_escaped, location, layer_id))
  return features


def GetLocationFromXmlItem(item):
  lat = lon = ''
  try:
    if item.find('.//coordinates') is not None:
      lon, lat = GetText(item.find('.//coordinates')).split(',')[:2]
    if item.find('.//point') is not None:
      lat, lon = GetText(item.find('.//point')).split()[:2]
    location = ndb.GeoPt(float(lat), float(lon))
    return location
  except ValueError:
    return None


def GetKmlUrl(root_url, layer):
  """Forms the URL that gets the KML for a given KML-powered layer."""
  layer_type = layer.get('type')
  if layer_type not in [maproot.LayerType.KML,
                        maproot.LayerType.GEORSS,
                        maproot.LayerType.GOOGLE_SPREADSHEET,
                        maproot.LayerType.GEOJSON,
                        maproot.LayerType.CSV]:
    return None

  source = (layer.get('source', {}).values() or [{}])[0]
  url = source.get('url')
  if layer_type in [maproot.LayerType.KML, maproot.LayerType.GEORSS]:
    return url or None

  if layer_type == maproot.LayerType.GOOGLE_SPREADSHEET:
    match = re.search(r'spreadsheet/.*[?&]key=(\w+)', url)
    url = match and GOOGLE_SPREADSHEET_CSV_URL.replace('$key', match.group(1))

  # See http://goto.google.com/kmlify for details on kmlify's query params.
  if url:
    params = [('url', url)]
    if layer_type == maproot.LayerType.GEOJSON:
      params += [('type', 'geojson')]
    else:
      lat, lon = source.get('latitude_field'), source.get('longitude_field')
      if not (lat and lon):
        return None
      params += [('type', 'csv'),
                 ('loc', lat == lon and lat or lat + ',' + lon),
                 ('icon', source.get('icon_url_template')),
                 ('color', source.get('color_template')),
                 ('hotspot', source.get('hotspot_template'))]
    params += [('name', source.get('title_template')),
               ('desc', source.get('description_template')),
               ('cond', source.get('condition0')),
               ('cond', source.get('condition1')),
               ('cond', source.get('condition2'))]
    return (root_url + '/.kmlify?' +
            urllib.urlencode([(k, v) for k, v in params if v]))


def GetGeoPt(place):
  """Returns a geo location of a given place.

  Args:
    place: Google Places API place

  Returns:
    GeoPt corresponding to the place location
  """
  location = place['geometry']['location']
  return ndb.GeoPt(location['lat'], location['lng'])


def GetFeaturesFromPlacesLayer(layer, location):
  """Builds a list of Feature objects for the Places layer near given location.

  Args:
    layer: Places layer that defines the criteria for places query
    location: db.GeoPt around which to retrieve places
  Returns:
    A list of Feature objects representing Google Places.
  """
  # Fetch JSON from the Places API nearby search
  places_layer = layer.get('source').get('google_places')
  request_params = [
      ('location', location),
      ('rankby', 'distance'),
      ('keyword', places_layer.get('keyword')),
      ('name', places_layer.get('name')),
      ('types', places_layer.get('types'))]
  place_results = GetPlacesApiResults(PLACES_API_SEARCH_URL, request_params,
                                      'results')

  # Convert Places API results to Feature objects
  features = []
  for place in place_results:
    # Delay building description_html until after features list was trimmed.
    # Otherwise, we'd be doing wasteful calls to Places API
    # to get address/phone number that will never get displayed.
    features.append(Feature(place['name'], None, GetGeoPt(place),
                            layer.get('id'), layer_type=layer.get('type'),
                            gplace_id=place['place_id']))
  return features


def GetGooglePlaceDetails(place_id):
  return GetPlacesApiResults(PLACES_API_DETAILS_URL, [('placeid', place_id)])


def GetGooglePlaceDescriptionHtml(place_details):
  # TODO(user): build a shorter address format (will require i18n)
  result = place_details.get('result')
  return ('<div>%s</div><div>%s</div>' %
          (result.get('formatted_address', ''),
           result.get('formatted_phone_number', '')))


def GetGooglePlaceHtmlAttributions(place_details):
  return place_details.get('html_attributions', [])


def GetPlacesApiResults(base_url, request_params, result_key_name=None):
  """Fetches results from Places API given base_url and request params.

  Args:
    base_url: URL prefix to use before the request params
    request_params: An array of key and value pairs for the request
    result_key_name: Name of the results field in the Places API response
        or None if the whole response should be returned
  Returns:
    Value for the result_key_name in the Places API response or all of the
    response if result_key_name is None
  """
  google_api_server_key = config.Get('google_api_server_key')
  if not google_api_server_key:
    raise base_handler.Error(
        500, 'google_api_server_key is not set in the config')
  request_params += [('key', google_api_server_key)]
  url = base_url + urllib.urlencode([(k, v) for k, v in request_params if v])

  # Call Places API if cache doesn't have a corresponding entry for the url
  response = JSON_PLACES_API_CACHE.Get(
      url, lambda: urlfetch.fetch(url=url, deadline=DEADLINE))

  # Parse JSON results
  response_content = json.loads(response.content)
  status = response_content.get('status')
  if status != 'OK' and status != 'ZERO_RESULTS':
    # Something went wrong with the request, log the error
    logging.error('Places API request [%s] failed with error %s', url, status)
    return []
  return (response_content.get(result_key_name) if result_key_name
          else response_content)


def GetTopic(root, topic_id):
  return {topic['id']: topic for topic in root['topics']}.get(topic_id)


def GetLayer(root, layer_id):
  return {layer['id']: layer for layer in root['layers']}.get(layer_id)


def GetFeatures(map_root, map_version_id, topic_id, request, location_center):
  """Gets a list of Feature objects for a given topic.

  Args:
    map_root: A dictionary with all the topics and layers information
    map_version_id: ID of the map version
    topic_id: ID of the crowd report topic; features are retrieved from the
        layers associated with this topic
    request: Original card request
    location_center: db.GeoPt around which to retrieve features. So far only
        Places layer uses this to narrow results according to the
        distance from this location. All other layers ignore this for now
        and just return all features. Note that Places layer doesn't have a set
        radius around location_center, it just tries to find features
        as close as possible to location_center.

  Returns:
    A list of Feature objects associated with layers of a given topic in a given
    map.
  """
  topic = GetTopic(map_root, topic_id) or {}
  features = []
  for layer_id in topic.get('layer_ids', []):
    layer = GetLayer(map_root, layer_id)
    if layer.get('type') == maproot.LayerType.GOOGLE_PLACES:
      features += GetFeaturesFromPlacesLayer(layer, location_center)
    else:
      url = GetKmlUrl(request.root_url, layer or {})
      if url:
        try:
          def GetXmlFeatures():
            content = kmlify.FetchData(url, request.host)
            return GetFeaturesFromXml(content, layer_id)
          features += XML_FEATURES_CACHE.Get(
              [url, map_root['id'], map_version_id, layer_id], GetXmlFeatures)
        except (SyntaxError, urlfetch.DownloadError):
          pass
  return features


def SetDistanceOnFeatures(features, center):
  for f in features:
    f.distance = EarthDistance(center, f.location)


def FilterFeatures(features, radius, max_count):
  # TODO(kpy): A top-k selection algorithm could be faster than O(n log n)
  # sort.  It seems likely to me that the gain would be small enough that it's
  # not worth the code complexity, but it wouldn't hurt to check my hunch.
  features.sort()  # sorts by distance; see Feature.__cmp__
  features[:] = [f for f in features[:max_count] if f.distance < radius]


def GetFilteredFeatures(map_root, map_version_id, topic_id, request,
                        center, radius, max_count):
  """Gets a list of the Feature objects for a topic within the given circle."""
  def GetFromDatastore():
    features = GetFeatures(map_root, map_version_id, topic_id, request, center)
    if center:
      SetDistanceOnFeatures(features, center)
    FilterFeatures(features, radius, max_count)
    # For the features that were selected for display, fetch additional details
    # that we avoid retrieving for unfiltered results due to latency concerns
    SetDetailsOnFilteredFeatures(features)
    return features

  return FILTERED_FEATURES_CACHE.Get(
      [map_root['id'], map_version_id, topic_id,
       center and RoundGeoPt(center), radius, max_count], GetFromDatastore)


def SetDetailsOnFilteredFeatures(features):
  # TODO(user): consider fetching details for each feature in parallel
  for f in features:
    if f.layer_type == maproot.LayerType.GOOGLE_PLACES:
      place_details = GetGooglePlaceDetails(f.gplace_id)
      f.description_html = GetGooglePlaceDescriptionHtml(place_details)
      f.html_attrs = GetGooglePlaceHtmlAttributions(place_details)


def GetAnswersAndReports(map_id, topic_id, location, radius):
  """Gets information on recent crowd reports for a given topic and location.

  Args:
    map_id: The map ID.
    topic_id: The topic ID.
    location: The location to search near, as an ndb.GeoPt.
    radius: Radius in metres.
  Returns:
    A 3-tuple (latest_answers, answer_times, report_dicts) where latest_answers
    is a dictionary {qid: latest_answer} containing the latest answer for each
    question; answer_times is a dictionary {qid: effective_time} giving the
    effective time of the latest answer for each question; and report_dicts is
    an array of dictionaries representing the latest 10 crowd reports.  Each
    dictionary in report_dicts has the answers for a report keyed by qid, as
    well as two special keys: '_effective' for the effective time and '_id'
    for the report ID.
  """
  full_topic_id = map_id + '.' + topic_id
  answers, answer_times, report_dicts = {}, {}, []
  now = datetime.datetime.utcnow()
  # Assume that all the most recently effective still-relevant answers are
  # contained among the 100 most recently updated CrowdReport entities.
  for report in model.CrowdReport.GetByLocation(
      location, {full_topic_id: radius}, 100, hidden=False):
    if now - report.effective < MAX_ANSWER_AGE:
      report_dict = {}
      # The report's overall comment is stored under the special qid '_text'.
      for question_id, answer in report.answers.items() + [
          (full_topic_id + '._text', report.text)]:
        tid, qid = question_id.rsplit('.', 1)
        if tid == full_topic_id:
          report_dict[qid] = answer
          if answer or answer == 0:  # non-empty answer
            if qid not in answer_times or report.effective > answer_times[qid]:
              answers[qid] = answer
              answer_times[qid] = report.effective
      report_dicts.append(
          dict(report_dict, _effective=report.effective, _id=report.id))
  report_dicts.sort(key=lambda report_dict: report_dict['_effective'])
  report_dicts.reverse()
  return answers, answer_times, report_dicts[:REPORTS_PER_FEATURE]


def GetLegibleTextColor(background_color):
  """Decides whether text should be black or white over a given color."""
  rgb = background_color.strip('#')
  if len(rgb) == 3:
    rgb = rgb[0]*2 + rgb[1]*2 + rgb[2]*2
  red, green, blue = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
  luminance = red * 0.299 + green * 0.587 + blue * 0.114
  return luminance > 128 and '#000' or '#fff'


def GetFeatureAttributions(features):
  """Builds a list of all unique html attributions for given features."""
  # Skip all the duplicates when joining attributions into one list
  html_attrs = set()
  for f in features:
    for attr in f.html_attrs:
      html_attrs.add(attr)
  return list(html_attrs)


def SetAnswersAndReportsOnFeatures(features, map_root, topic_id, qids):
  """Sets 'status_color', 'answers', and 'answer_text' on the given Features."""
  map_id = map_root.get('id') or ''
  topic = GetTopic(map_root, topic_id) or {}
  radius = topic.get('cluster_radius', 100)

  questions_by_id = {q['id']: q for q in topic.get('questions', [])}
  choices_by_id = {(q['id'], c['id']): c
                   for q in topic.get('questions', [])
                   for c in q.get('choices', [])}

  def FormatAnswers(answers):
    """Formats a set of answers into a text summary."""
    answer_texts = []
    for qid in qids:
      question = questions_by_id.get(qid)
      answer = answers.get(qid)
      prefix = question and question.get('title') or ''
      prefix += ': ' if prefix else ''
      if question:
        if question.get('type') == 'CHOICE':
          choice = choices_by_id.get((qid, answer))
          if choice:
            label = choice.get('label') or prefix + choice.get('title', '')
            answer_texts.append(label + '.')
        elif answer or answer == 0:
          answer_texts.append(prefix + str(answer) + '.')
    return ' '.join(answer_texts)

  def GetStatusColor(answers):
    """Determines the indicator color from the answer to the first question."""
    qid = qids and qids[0] or ''
    first_question = questions_by_id.get(qid, {})
    if first_question.get('type') == 'CHOICE':
      choice = choices_by_id.get((qid, answers.get(qid)))
      return choice and choice.get('color')

  if topic.get('crowd_enabled') and qids:
    for f in features:
      # Even though we use the radius to get the latest answers, the cache key
      # omits radius so that InvalidateReportCache can quickly delete cache
      # entries without fetching from the datastore.  So, when a cluster radius
      # is changed and its map is republished, affected entries in the answer
      # cache will be stale until they expire.  This seems like a good tradeoff
      # because (a) changing a cluster radius in a published map is rare (less
      # than once per map); (b) the answer cache has a short TTL (15 s); and
      # (c) posting crowd reports is frequent (many times per day).
      answers, answer_times, report_dicts = REPORT_CACHE.Get(
          [map_id, topic_id, RoundGeoPt(f.location)],
          lambda: GetAnswersAndReports(map_id, topic_id, f.location, radius))
      f.answers = answers
      f.answer_text = FormatAnswers(answers)
      if answer_times:
        # Convert datetimes to string descriptions like "5 min ago".
        f.answer_time = utils.ShortAge(max(answer_times.values()))
      f.answer_source = 'Crisis Map user'
      f.status_color = GetStatusColor(answers)

      # Include a few recent reports.
      f.reports = [{'answer_summary': FormatAnswers(report),
                    'effective': utils.ShortAge(report['_effective']),
                    'id': report['_id'],
                    'text': '_text' in qids and report['_text'] or '',
                    'status_color': GetStatusColor(report)}
                   for report in report_dicts]


def InvalidateReportCache(full_topic_ids, location):
  """Deletes cached answers affected by a new report at a given location."""
  for full_topic_id in full_topic_ids:
    if '.' in full_topic_id:
      map_id, topic_id = full_topic_id.split('.')
      REPORT_CACHE.Delete([map_id, topic_id, RoundGeoPt(location)])


def RemoveParamsFromUrl(url, *params):
  """Removes specified query parameters from a given URL."""
  base, query = (url.split('?') + [''])[:2]
  pairs = cgi.parse_qsl(query)
  query = urllib.urlencode([(k, v) for k, v in pairs if k not in params])
  # Returned URL always ends in ? or a query parameter, so that it's always
  # safe to add a parameter by appending '&name=value'.
  return base + '?' + query


def GetGeoJson(features, include_descriptions):
  """Converts a list of Feature instances to a GeoJSON object."""
  return {
      'type': 'FeatureCollection',
      'features': [{
          'type': 'Feature',
          'geometry': {
              'type': 'Point',
              'coordinates': [f.location.lon, f.location.lat]
          },
          'properties': {
              'name': f.name,
              'description_html':
                  f.description_html if include_descriptions else None,
              'distance': f.distance,
              'distance_mi': RoundDistance(f.distance_mi),
              'distance_km': RoundDistance(f.distance_km),
              'layer_id': f.layer_id,
              'status_color': f.status_color,
              'answer_text': f.answer_text,
              'answer_time': f.answer_time,
              'answer_source': f.answer_source,
              'answers': f.answers,
              'reports': f.reports
          }
      } for f in features]
  }


def RoundDistance(distance):
  """Round distances above 10 (mi/km) to the closest integer."""
  return math.ceil(distance) if distance > 10 else distance


def RenderFooter(items, html_attrs=None):
  """Renders the card footer as HTML and appends source attributions at the end.

  Args:
    items: A sequence of items, where each item is (a) a string or (b) a pair
        [url, text] to be rendered as a hyperlink that opens in a new window.
    html_attrs: A list of html strings with links to attributions.
  Returns:
    A Unicode string of safe HTML containing only text and <a> tags.
  """
  results = []
  for item in items:
    if isinstance(item, (str, unicode)):
      results.append(kmlify.HtmlEscape(item))
    elif len(item) == 2:
      url, text = item
      scheme, _, _, _, _ = urlparse.urlsplit(url)
      if scheme in ['http', 'https']:  # accept only safe schemes
        results.append('<a href="%s" target="_blank">%s</a>' % (
            kmlify.HtmlEscape(url), kmlify.HtmlEscape(text)))
  for html_attr in html_attrs or []:
    # Attributions html comes from Google Places API or built on the fly
    # inside card.py, so no sanitization here
    results.append(html_attr)
  return ' '.join(results)


class CardBase(base_handler.BaseHandler):
  """Card rendering code common to all the card handlers below.

  For all these card handlers, the map and topic are determined from the
  URL path (the map is specified by ID or label, and the topic ID is either
  explicit in the path or assumed to be the first existing topic for the map).
  Use these query parameters to customize the resulting card:
    - n: Maximum number of items to show.
    - ll: Geolocation of the center point to search near (in lat,lon format).
    - r: Search radius in metres.
    - output: If 'json', response is returned as GeoJSON; otherwise,
      it is rendered as HTML.
    - unit: Distance unit to show (either 'km' or 'mi').
    - qids: Comma-separated IDs of questions within the topic.  Short text
          descriptions of the most recently crowd-reported answers to these
          questions are shown with each item.  The first question in qids is
          treated specially: if it is a CHOICE question, its answer will also
          be displayed as a coloured status dot.
    - places: A specification of the list of possible locations for which the
          card has content, specified as a JSON array of objects, each with
          the following keys: "id", "name", and "ll". Example:
            [{"id":"place1", "name":"Centerville", "ll":[10.0,-120.0]},
             {"id": "place2", "name":"Springfield", "ll":[10.5,-120.5"]}]
    - place: A place ID, expected to be one of the "ids" of the provided
          '?places' array. If the given place ID is invalid, a default place
          is used (the first place in the ?places array). If a valid '?ll' is
          provided, the '?place' parameter is ignored.
    - show_desc: If true, then display descriptions under place names.
          Otherwise, just display the place name.
    - footer: Text and links for the footer, specified as a JSON array where
          each element is either a plain text string or a two-element array
          [url, text], which is rendered as a link.
  """
  embeddable = True
  error_template = 'card-error.html'

  def GetForMap(self, map_root, map_version_id, topic_id, map_label=None):
    """Renders the card for a particular map and topic.

    Args:
      map_root: The MapRoot dictionary for the map.
      map_version_id: The version ID of the MapVersionModel (for a cache key).
      topic_id: The topic ID.
      map_label: The label of the published map (for analytics).
    """
    topic = GetTopic(map_root, topic_id)
    if not topic:
      raise base_handler.Error(404, 'No such topic.')
    output = str(self.request.get('output', ''))
    lat_lon = str(self.request.get('ll', ''))
    max_count = int(self.request.get('n', 5))  # number of results to show
    radius = float(self.request.get('r', 100000))  # radius, metres
    unit = str(self.request.get('unit', self.GetDistanceUnitForCountry()))
    qids = self.request.get('qids').replace(',', ' ').split()
    places_json = self.request.get('places') or '[]'
    place_id = str(self.request.get('place', ''))
    footer_json = self.request.get('footer') or '[]'
    location_unavailable = self.request.get('location_unavailable')
    lang = base_handler.SelectLanguageForRequest(self.request, map_root)
    include_descriptions = self.request.get('show_desc')

    try:
      places = json.loads(places_json)
    except ValueError:
      logging.error('Could not parse ?places= parameter')
    try:
      footer = json.loads(footer_json)
    except ValueError:
      logging.error('Could not parse ?footer= parameter')

    # If '?ll' parameter is supplied, find nearby results.
    center = None
    if lat_lon:
      try:
        lat, lon = lat_lon.split(',')
        center = ndb.GeoPt(float(lat), float(lon))
      except ValueError:
        logging.error('Could not extract center for ?ll parameter')

    # If neither '?ll' nor '?place' parameters are given, or if ?place
    # value is invalid, use a default place.
    place = None
    if not center:
      place = {p['id']: p for p in places}.get(place_id, places and places[0])

    # If '?place' parameter is supplied, use it as the center of the
    # point-radius query.
    if not center and place:
      try:
        lat, lon = place['ll']
        center = ndb.GeoPt(lat, lon)
      except (KeyError, TypeError, ValueError):
        logging.error('Could not extract center for ?place=%s', place_id)

    try:
      # Find POIs associated with the topic layers
      features = GetFilteredFeatures(
          map_root, map_version_id, topic_id, self.request,
          center, radius, max_count)
      html_attrs = GetFeatureAttributions(features)
      SetAnswersAndReportsOnFeatures(features, map_root, topic_id, qids)
      geojson = GetGeoJson(features, include_descriptions)
      geojson['properties'] = {
          'map_id': map_root.get('id'),
          'topic': topic,
          'html_attrs': html_attrs,
          'unit': unit
      }
      if output == 'json':
        self.WriteJson(geojson)
      else:
        self.response.out.write(self.RenderTemplate('card.html', {
            'features': geojson['features'],
            'title': topic.get('title', ''),
            'unit': unit,
            'lang': lang,
            'url_no_unit': RemoveParamsFromUrl(self.request.url, 'unit'),
            'place': place,
            'config_json': json.dumps({
                'url_no_loc': RemoveParamsFromUrl(
                    self.request.url, 'll', 'place'),
                'place': place,
                'location_unavailable': bool(location_unavailable),
                'map_id': map_root.get('id', ''),
                'topic_id': topic_id,
                'map_label': map_label or '',
                'topic_title': topic.get('title', '')
            }),
            'places_json': json.dumps(places),
            'footer_html': RenderFooter(footer or [], html_attrs)
        }))

    except Exception, e:  # pylint:disable=broad-except
      logging.exception(e)

  def GetDistanceUnitForCountry(self):
    unit = self.request.get('unit', '')
    if unit in ['mi', 'km']:
      return unit
    elif unit:
      logging.error('Could not parse unit: should be mi or km')
    country_code = self.request.headers.get('X-AppEngine-Country', '')
    return utils.GetDistanceUnitsForCountry(country_code)


class CardByIdAndTopic(CardBase):
  """Produces a card given a map ID and topic ID."""

  def Get(self, map_id, topic_id, user=None, domain=None):
    m = model.Map.Get(map_id)
    if not m:
      raise base_handler.Error(404, 'No such map.')
    self.GetForMap(m.map_root, m.current_version_id, topic_id)


class CardByLabelAndTopic(CardBase):
  """Produces a card given a published map label and topic ID."""

  def Get(self, label, topic_id, user=None, domain=None):
    domain = domain or config.Get('primary_domain') or ''
    entry = model.CatalogEntry.Get(domain, label)
    if not entry:
      raise base_handler.Error(404, 'No such map.')
    self.GetForMap(entry.map_root, entry.map_version_id, topic_id, label)

  def Post(self, label, topic_id, user=None, domain=None):
    self.Get(label, topic_id, user, domain)


class CardByLabel(CardBase):
  """Redirects to the first topic for the specified map."""

  def Get(self, label, user=None, domain=None):
    domain = domain or config.Get('primary_domain') or ''
    entry = model.CatalogEntry.Get(domain, label)
    if not entry:
      raise base_handler.Error(404, 'No such map.')
    topics = entry.map_root.get('topics', [])
    if not topics:
      raise base_handler.Error(404, 'Map has no topics.')
    self.redirect('%s/%s' % (label, str(topics[0]['id'])))
