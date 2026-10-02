// vim: set et sw=2 ts=2 sts=2 ff=unix fenc=utf8:
// QD AI 智能识别签到 控制器
(function() {
  define(function(require, exports, module) {
    var analysis = require('/static/har/analysis');
    var utils = require('/static/components/utils');

    return angular.module('ai_ctrl', []).controller('AIAnalyzeCtrl', function($scope, $rootScope, $http) {
      $scope.ai_enabled = false;
      $scope.ai_model = '';
      $scope.hint = '';
      $scope.error = '';
      $scope.error_type = ''; // 'disabled' | 'skipped' | 'failed'
      $scope.result = null;
      $scope.result_text = '';
      $scope.warnings = [];
      $scope.show_warnings = true;
      $scope.show_raw = false;
      $scope.busy = false;

      // 初始查询 AI 状态
      $http.get('/har/ai_status').then(function(res) {
        $scope.ai_enabled = !!(res.data && res.data.enabled);
        $scope.ai_model = (res.data && res.data.model) || '';
      }, function() {
        $scope.ai_enabled = false;
        $scope.ai_model = '';
      });

      $scope.ai_open = function() {
        $scope.error = '';
        $scope.error_type = '';
        $scope.result = null;
        $scope.result_text = '';
        $scope.warnings = [];
        $scope.show_warnings = true;
        $scope.show_raw = false;
      };

      $scope.toggle_warnings = function() {
        $scope.show_warnings = !$scope.show_warnings;
      };

      $scope.toggle_raw = function() {
        $scope.show_raw = !$scope.show_raw;
      };

      // 从全局或本地存储安全获取 HAR
      function collect_har() {
        var src = (window.global_har && window.global_har.har) ? window.global_har.har : null;
        if (!src && utils.storage && utils.storage.get) {
          src = utils.storage.get('har_har');
        }
        if (!src) return null;
        return src;
      }

      function classify_error(msg) {
        if (!msg) return 'failed';
        if (msg.indexOf('未配置') !== -1 || msg.indexOf('未启用') !== -1 || msg.indexOf('API_KEY') !== -1) {
          return 'disabled';
        }
        if (msg.indexOf('未找到可分析') !== -1 || msg.indexOf('均被过滤') !== -1 || msg.indexOf('跳过') !== -1) {
          return 'skipped';
        }
        return 'failed';
      }

      $scope.run = function() {
        if ($scope.busy) return;

        if (!$scope.ai_enabled) {
          $scope.error = 'AI 功能未启用：请管理员配置 AI_API_KEY 环境变量后重启服务';
          $scope.error_type = 'disabled';
          return;
        }

        var har = collect_har();
        if (!har || !har.log || !har.log.entries || har.log.entries.length === 0) {
          $scope.error = '当前没有 HAR 请求数据，请先上传、录制或打开 HAR 模板后再使用 AI 分析';
          $scope.error_type = 'skipped';
          return;
        }

        $scope.error = '';
        $scope.error_type = '';
        $scope.result = null;
        $scope.result_text = '';
        $scope.warnings = [];
        $scope.busy = true;

        $http.post('/har/ai_analyze', {har: har, hint: $scope.hint || ''}).then(function(res) {
          $scope.busy = false;
          if (!res.data || !res.data.ok) {
            var errMsg = (res.data && res.data.error) || 'AI 分析失败';
            $scope.error = errMsg;
            $scope.error_type = classify_error(errMsg);
            $scope.result_text = '';
            return;
          }
          $scope.result = res.data;
          $scope.warnings = res.data.warnings || [];
          $scope.show_warnings = ($scope.warnings.length > 0);

          try {
            $scope.result_text = JSON.stringify(res.data.result, null, 2);
          } catch (e) {
            $scope.result_text = String(res.data.result);
          }
        }, function(res) {
          $scope.busy = false;
          var msg = '请求失败';
          if (res && res.data && res.data.error) {
            msg = res.data.error;
          } else if (res && res.status) {
            msg = 'HTTP ' + res.status;
          }
          $scope.error = msg;
          $scope.error_type = classify_error(msg);
          $scope.result_text = '';
        });
      };

      $scope.retry = function() {
        $scope.run();
      };

      // 把 AI 给出的精简 HAR 应用到编辑器
      $scope.apply = function() {
        if (!$scope.result || !$scope.result.har) return;
        var harData = $scope.result.har;
        var rawHar;

        // 如果是 QD 模板数组格式，使用 utils.tpl2har 转换为标准 HAR 结构
        if (Array.isArray(harData)) {
          rawHar = utils.tpl2har(harData);
        } else if (harData && harData.log) {
          rawHar = harData;
        } else {
          rawHar = utils.tpl2har([harData]);
        }

        var analyzedHar = analysis.analyze(rawHar, {});
        var entryCount = (analyzedHar && analyzedHar.log && analyzedHar.log.entries) ? analyzedHar.log.entries.length : 0;
        var sitename = ($scope.result.result && $scope.result.result.sitename) || 'AI 生成模板';

        var loaded = {
          filename: sitename,
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
          message: '已成功应用 AI 生成的模板，已覆盖当前编辑器条目（保留 ' + entryCount + ' 条关键请求）。'
        });

        angular.element('#ai-analyze').modal('hide');
      };
    });
  });
}).call(this);
