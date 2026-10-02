// vim: set et sw=2 ts=2 sts=2 ff=unix fenc=utf8:
// QD URL 自动抓包 控制器 (Playwright sidecar)
(function() {
  define(function(require, exports, module) {
    var analysis = require('/static/har/analysis');
    var utils = require('/static/components/utils');

    return angular.module('auto_capture_ctrl', []).controller('AutoCaptureCtrl', function($scope, $rootScope, $http) {
      $scope.enabled = false;
      $scope.url = '';
      $scope.cookies = '';
      $scope.storage_state = '';   // 用户粘贴 storage_state.json 内容
      $scope.hint = '';
      $scope.selector = '';
      $scope.auto_analyze = true;
      $scope.busy = false;
      $scope.error = '';
      $scope.result = null;
      $scope.candidates = [];

      $http.get('/har/auto_capture_status').then(function(res) {
        $scope.enabled = !!(res.data && res.data.enabled);
      }, function() {
        $scope.enabled = false;
      });

      $scope.open = function() {
        $scope.error = '';
        $scope.result = null;
        $scope.candidates = [];
      };

      $scope.run = function() {
        if ($scope.busy) return;

        if (!$scope.url) {
          $scope.error = '请填写抓包目标 URL';
          return;
        }
        var payload = {
          url: $scope.url,
          hint: $scope.hint || '',
          auto_analyze: !!$scope.auto_analyze
        };
        if ($scope.selector) payload.selector = $scope.selector;
        if ($scope.storage_state && $scope.storage_state.trim()) {
          try {
            payload.storage_state = JSON.parse($scope.storage_state);
          } catch (e) {
            $scope.error = 'storage_state 不是合法 JSON 格式: ' + e.message;
            return;
          }
        } else if ($scope.cookies) {
          payload.cookies = $scope.cookies;
        }

        $scope.busy = true;
        $scope.error = '';
        $scope.result = null;
        $scope.candidates = [];

        $http.post('/har/auto_capture', payload).then(function(res) {
          $scope.busy = false;
          if (!res.data || !res.data.ok) {
            $scope.error = (res.data && res.data.error) || '自动抓包失败';
            $scope.candidates = (res.data && res.data.candidates) || [];
            return;
          }
          $scope.result = res.data;
          $scope.candidates = res.data.candidates || [];
        }, function(res) {
          $scope.busy = false;
          $scope.error = (res && res.data && res.data.error) || ('HTTP ' + (res && res.status) + ' 请求异常');
        });
      };

      // 把 sidecar 抓到的 HAR 直接载入编辑器（如有 AI 优化版优先用它）
      $scope.apply = function() {
        if (!$scope.result) return;
        var har = ($scope.result.ai && $scope.result.ai.har) || $scope.result.har;
        if (!har) return;

        var rawHar;
        if (Array.isArray(har)) {
          rawHar = utils.tpl2har(har);
        } else if (har && har.log) {
          rawHar = har;
        } else {
          rawHar = utils.tpl2har([har]);
        }

        var analyzedHar = analysis.analyze(rawHar, {});
        var entryCount = (analyzedHar && analyzedHar.log && analyzedHar.log.entries) ? analyzedHar.log.entries.length : 0;
        var isAiApplied = !!($scope.result.ai && $scope.result.ai.har);

        var loaded = {
          filename: ($scope.result.ai && $scope.result.ai.result && $scope.result.ai.result.sitename)
                    || $scope.url
                    || '自动抓包',
          har: analyzedHar,
          upload: true
        };
        loaded.env = {};
        var vars = analysis.find_variables(loaded.har) || [];
        for (var i = 0; i < vars.length; i++) {
          loaded.env[vars[i]] = '';
        }

        $rootScope.$emit('har-loaded', loaded);
        $rootScope.$broadcast('editor-alert', {
          type: 'success',
          message: isAiApplied
            ? ('已应用自动抓包与 AI 识别结果，已覆盖当前编辑器条目（保留 ' + entryCount + ' 条关键请求）。')
            : ('已应用自动抓包结果，已覆盖当前编辑器条目（共 ' + entryCount + ' 条请求）。')
        });

        angular.element('#auto-capture').modal('hide');
      };

      $scope.use_candidate_selector = function(cand) {
        $scope.selector = cand.selector;
      };
    });
  });
}).call(this);
