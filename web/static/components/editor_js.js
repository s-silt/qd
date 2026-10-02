// vim: set et sw=2 ts=2 sts=2 ff=unix fenc=utf8:
// HAR 编辑器辅助脚本
function reserve_check() {
  var scope = angular.element('#entries').scope();
  if (scope && typeof scope.inverse === 'function') {
    scope.$apply(function() {
      scope.inverse();
    });
    return;
  }
  if (window.global_har && window.global_har.har && window.global_har.har.log && window.global_har.har.log.entries) {
    var entries = window.global_har.har.log.entries;
    for (var i = 0; i < entries.length; i++) {
      entries[i].checked = !entries[i].checked;
    }
  }
}