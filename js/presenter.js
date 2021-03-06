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
 * @fileoverview The Presenter translates user actions into effects on the
 *     application.  User actions are events emitted from Views; the resulting
 *     effects are changes in the AppState (application state) or manipulation
 *     of Views that cause changes in the AppState.  The Presenter handles
 *     just the read-only actions; actions that edit the document are handled
 *     by the EditPresenter, which will be separately loaded.
 * @author kpy@google.com (Ka-Ping Yee)
 */
goog.provide('cm.Presenter');

goog.require('cm');
goog.require('cm.Analytics');
goog.require('cm.AppState');
goog.require('cm.LatLonBox');
goog.require('cm.MapView');
goog.require('cm.PanelView');
goog.require('cm.TabPanelView');
goog.require('cm.events');
goog.require('cm.ui');

/** Default zoom level for "my location" button clicks */
/** @const */var DEFAULT_MY_LOCATION_ZOOM_LEVEL = 11;

/**
 * The Presenter translates user actions into effects on the application,
 * and also logs those actions as Analytics events.
 * @param {cm.AppState} appState The application state model.
 * @param {cm.MapView} mapView The map view.
 * @param {cm.PanelView|cm.TabPanelView} panelView The panel view.
 * @param {Element} panelElem The panel element.
 * @param {string} mapId The map ID, for logging with Analytics.
 * @constructor
 */
cm.Presenter = function(appState, mapView, panelView, panelElem, mapId) {
  /**
   * @type cm.AppState
   * @private
   */
  this.appState_ = appState;

  /**
   * @type cm.MapView
   * @private
   */
  this.mapView_ = mapView;

  /**
   * @type string
   * @private
   */
  this.mapId_ = mapId;

  /**
   * The currently selected feature position.
   * @private {google.maps.LatLng}
   */
  this.focusPosition_ = null;

  cm.events.listen(cm.app, cm.events.RESET_VIEW, function(event) {
    this.resetView(event.model);
  }, this);

  cm.events.listen(panelView, cm.events.TOGGLE_LAYER, function(event) {
    appState.setLayerEnabled(event.id, event.value);
  }, this);

  cm.events.listen(cm.app, cm.events.CHANGE_OPACITY, function(event) {
    appState.setLayerOpacity(event.id, event.opacity);
  });

  cm.events.listen(panelView, cm.events.ZOOM_TO_LAYER, function(event) {
    appState.setLayerEnabled(event.id, true);
    mapView.zoomToLayer(event.id);
    cm.events.emit(panelElem, 'panelclose');
  }, this);

  cm.events.listen(panelView, cm.events.SELECT_SUBLAYER, function(event) {
    appState.selectSublayer(event.model, event.id);
    appState.setLayerEnabled(event.model.get('id'), true);
  });

  // TODO(kpy): Listen for panelopen & panelclose, and open/close the
  // cm.PanelView here, consistent with the way
  // we handle other events.  At the moment, the cm.LayersButton emits an
  // event directly on the cm.PanelView's DOM element.

  // TODO(kpy): Open the cm.SharePopup in response to cm.events.SHARE_BUTTON,
  // consistent with the way we
  // handle other events.  At the moment, the cm.SharePopup is tightly
  // coupled to cm.ShareButton (cm.ShareButton owns a private this.popup_).

  cm.events.listen(cm.app, cm.events.GO_TO_MY_LOCATION, function(event) {
    this.zoomToUserLocation(DEFAULT_MY_LOCATION_ZOOM_LEVEL);
  }, this);

  cm.events.listen(panelView, cm.events.FILTER_QUERY_CHANGED, function(event) {
    // TODO(user): Figure out when to log an analytics event
    // (after a delay, on a backspace press, etc) so we don't
    // get an analytics log per keypress.
    appState.setFilterQuery(event.query);
    this.logFilterQueryChange_(event.query, 1000);
  }, this);
  cm.events.listen(panelView, cm.events.FILTER_MATCHES_CHANGED,
    function(event) {
      appState.setMatchedLayers(event.matches);
  });

  if (panelView instanceof cm.TabPanelView) {
    cm.events.listen(mapView, cm.events.SELECT_FEATURE, function(event) {
      this.focusPosition_ = null;  // prevent recentering on previous feature
      var mapShouldPan = panelView.isBelowMap() && !panelView.isExpanded();
      panelView.selectFeature(event);
      if (mapShouldPan) {
        mapView.focusOnPoint(event.position);
      }
      this.focusPosition_ = event.position;
    }, this);
    cm.events.listen(mapView, cm.events.DESELECT_FEATURE, function() {
      panelView.deselectFeature();
      this.focusPosition_ = null;
    }, this);
    cm.events.listen(cm.app, cm.events.DETAILS_TAB_OPENED, function() {
      var viewport = /** @type cm.LatLonBox */(mapView.get('viewport'));
      if (this.focusPosition_ && !viewport.contains(this.focusPosition_)) {
        mapView.focusOnPoint(this.focusPosition_);
      }
    }, this);
  }
};

/**
 * Resets the AppState and MapView according to the given MapModel. If a URI
 * is given, applies adjustments according to the query parameters.
 * @param {cm.MapModel} mapModel A map model.
 * @param {!goog.Uri|!Location|string=} opt_uri An optional URI whose query
 *     parameters are used to adjust the view settings.
 */
cm.Presenter.prototype.resetView = function(mapModel, opt_uri) {
  this.appState_.setFromMapModel(mapModel);
  this.mapView_.matchViewport(
      /** @type cm.LatLonBox */(mapModel.get('viewport')) ||
      cm.LatLonBox.ENTIRE_MAP);
  if (opt_uri) {
    this.mapView_.adjustViewportFromUri(opt_uri);
    this.appState_.setFromUri(opt_uri, mapModel);
  }
};

/**
 * Zoom to the user's geolocation.
 * @param {number} zoom The zoom level to apply when the geolocation is found.
 */
cm.Presenter.prototype.zoomToUserLocation = function(zoom) {
  var mapView = this.mapView_;
  if (navigator && navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(function(position) {
      mapView.set('zoom', zoom);
      mapView.set('center', new google.maps.LatLng(
          position.coords.latitude, position.coords.longitude));
    });
  }
};

/**
 * Logs a change in the filter query after the specified delay since the last
 * change.
 * @param {string} query The query value.
 * @param {number} delayMs Number of milliseconds since the last filter query
 *    change event to delay before logging the change.
 * @private
 */
cm.Presenter.prototype.logFilterQueryChange_ = function(query, delayMs) {
  if (this.filterQueryTimeoutId_) {
    goog.global.clearTimeout(this.filterQueryTimeoutId_);
  }
  var logFn = goog.bind(function() {
      this.filterQueryTimeoutId_ = null;
      if (query) {
        cm.Analytics.logAction(
            cm.Analytics.LayersTabAction.FILTER_QUERY_ENTERED, null);
      }
  }, this);
  this.filterQueryTimeoutId_ = goog.global.setTimeout(logFn, delayMs);
};
