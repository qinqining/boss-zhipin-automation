/**
 * 自动化向导页面 - 简化版（3步流程）
 * 步骤1: 启动浏览器
 * 步骤2: 手动操作引导（登录、选职位、配筛选）
 * 步骤3: 配置并启动打招呼
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { Zap, Monitor, CheckCircle2, PlayCircle, Loader2, X, Save, Hand } from 'lucide-react';
import { toast } from 'sonner';

import { useAutomation } from '@/hooks/useAutomation';
import { useAutomationTemplates } from '@/hooks/useAutomationTemplates';
import { useAccounts } from '@/hooks/useAccounts';
import { usePositionKeywords, type PositionKeyword } from '@/hooks/usePositionKeywords';
import type { GreetingStatus, GreetingLogEntry } from '@/types';
import type { UserAccount } from '@/types/account';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Textarea } from '@/components/ui/textarea';

type WizardStep = 'browser' | 'manual' | 'confirm';

export default function AutomationWizard() {
  const { initBrowser, checkReadyState } = useAutomation();
  const { createTemplate } = useAutomationTemplates();
  const { getAccounts } = useAccounts();
  const { searchKeywords, deleteKeyword } = usePositionKeywords();

  // 步骤状态
  const [currentStep, setCurrentStep] = useState<WizardStep>('browser');

  // 模板保存相关
  const [saveTemplateDialogOpen, setSaveTemplateDialogOpen] = useState(false);
  const [templateName, setTemplateName] = useState('');
  const [templateDescription, setTemplateDescription] = useState('');
  const [savingTemplate, setSavingTemplate] = useState(false);

  // 浏览器配置
  const [showBrowser, setShowBrowser] = useState(true); // 默认勾选，因为需要手动操作
  const [browserInitializing, setBrowserInitializing] = useState(false);

  // 账号相关状态
  const [availableAccounts, setAvailableAccounts] = useState<UserAccount[]>([]);
  const [selectedComId, setSelectedComId] = useState<string>('');
  const [accountsLoaded, setAccountsLoaded] = useState(false);

  // 手动操作引导 - 就绪状态
  const [readyState, setReadyState] = useState({
    logged_in: false,
    on_recommend_page: false,
    has_frame: false,
    needs_verification: false,
  });
  const [userInfo, setUserInfo] = useState<any>(null);
  const readyPollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 配置状态
  const [maxContacts, setMaxContacts] = useState<number | ''>(10);

  // 打招呼任务状态
  const [greetingStarted, setGreetingStarted] = useState(false);
  const [greetingStatus, setGreetingStatus] = useState<GreetingStatus | null>(null);
  const [greetingLogs, setGreetingLogs] = useState<GreetingLogEntry[]>([]);
  const pollingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 期望职位匹配相关状态
  const [expectedPositions, setExpectedPositions] = useState<string[]>([]);
  const [positionInput, setPositionInput] = useState('');
  const [keywordSuggestions, setKeywordSuggestions] = useState<PositionKeyword[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [selectedSuggestionIndex, setSelectedSuggestionIndex] = useState(-1);
  const suggestionsRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 页面加载时获取账号列表
  useEffect(() => {
    const loadAccounts = async () => {
      try {
        const accounts = await getAccounts();
        setAvailableAccounts(accounts);
      } catch (error) {
        console.error('加载账号列表失败:', error);
      } finally {
        setAccountsLoaded(true);
      }
    };
    loadAccounts();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * 初始化浏览器（手动模式）
   */
  const handleInitBrowser = async () => {
    setBrowserInitializing(true);
    try {
      const comId = selectedComId ? Number(selectedComId) : undefined;
      const result = await initBrowser(!showBrowser, comId, true);

      if (result.success) {
        if (comId) {
          toast.success('浏览器已启动并加载了账号登录状态');
        } else {
          toast.success('浏览器已启动，请在浏览器中登录');
        }
        setCurrentStep('manual');
        // 开始轮询就绪状态
        startReadyStatePolling();
      }
    } catch (error) {
      console.error('浏览器初始化失败:', error);
      toast.error('浏览器初始化失败');
    } finally {
      setBrowserInitializing(false);
    }
  };

  /**
   * 开始轮询就绪状态
   */
  const startReadyStatePolling = useCallback(() => {
    if (readyPollingRef.current) {
      clearInterval(readyPollingRef.current);
    }

    readyPollingRef.current = setInterval(async () => {
      try {
        const state = await checkReadyState();
        setReadyState({
          logged_in: state.logged_in,
          on_recommend_page: state.on_recommend_page,
          has_frame: state.has_frame,
          needs_verification: state.needs_verification || false,
        });

        if (state.user_info) {
          setUserInfo(state.user_info);
        }

        // 全部就绪后停止轮询
        if (state.ready) {
          if (readyPollingRef.current) {
            clearInterval(readyPollingRef.current);
            readyPollingRef.current = null;
          }
          toast.success('所有条件已满足，可以继续！');
        }
      } catch (error) {
        // 静默忽略轮询错误
      }
    }, 3000);
  }, [checkReadyState]);

  /**
   * 搜索关键词（防抖）
   */
  const handlePositionInputChange = (value: string) => {
    setPositionInput(value);
    setSelectedSuggestionIndex(-1);

    if (searchTimerRef.current) {
      clearTimeout(searchTimerRef.current);
    }

    if (value.trim()) {
      searchTimerRef.current = setTimeout(async () => {
        try {
          const results = await searchKeywords(value.trim());
          setKeywordSuggestions(results);
          setShowSuggestions(true);
        } catch {
          setKeywordSuggestions([]);
        }
      }, 300);
    } else {
      // 输入为空时也加载历史关键词
      searchTimerRef.current = setTimeout(async () => {
        try {
          const results = await searchKeywords('');
          setKeywordSuggestions(results);
          setShowSuggestions(results.length > 0);
        } catch {
          setKeywordSuggestions([]);
        }
      }, 300);
    }
  };

  /**
   * 添加期望职位
   */
  const handleAddPosition = (name?: string) => {
    const trimmed = (name || positionInput).trim();
    if (trimmed && !expectedPositions.includes(trimmed)) {
      setExpectedPositions([...expectedPositions, trimmed]);
      setPositionInput('');
      setShowSuggestions(false);
      setKeywordSuggestions([]);
      setSelectedSuggestionIndex(-1);
    } else if (expectedPositions.includes(trimmed)) {
      toast.warning('该职位已添加');
    }
  };

  /**
   * 删除期望职位
   */
  const handleRemovePosition = (index: number) => {
    setExpectedPositions(expectedPositions.filter((_, i) => i !== index));
  };

  /**
   * 删除历史关键词
   */
  const handleDeleteKeyword = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    try {
      await deleteKeyword(id);
      setKeywordSuggestions(prev => prev.filter(k => k.id !== id));
      toast.success('已删除历史关键词');
    } catch {
      toast.error('删除失败');
    }
  };

  /**
   * 处理键盘导航
   */
  const handlePositionKeyDown = (e: React.KeyboardEvent) => {
    const filteredSuggestions = keywordSuggestions.filter(
      k => !expectedPositions.includes(k.name)
    );
    const trimmed = positionInput.trim();
    const hasCreateOption = trimmed && !filteredSuggestions.some(k => k.name === trimmed);
    const totalItems = filteredSuggestions.length + (hasCreateOption ? 1 : 0);

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (showSuggestions && totalItems > 0) {
        setSelectedSuggestionIndex(prev =>
          prev < totalItems - 1 ? prev + 1 : 0
        );
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (showSuggestions && totalItems > 0) {
        setSelectedSuggestionIndex(prev =>
          prev > 0 ? prev - 1 : totalItems - 1
        );
      }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (selectedSuggestionIndex >= 0 && showSuggestions) {
        if (selectedSuggestionIndex < filteredSuggestions.length) {
          handleAddPosition(filteredSuggestions[selectedSuggestionIndex].name);
        } else if (hasCreateOption) {
          handleAddPosition(trimmed);
        }
      } else {
        handleAddPosition();
      }
    } else if (e.key === 'Escape') {
      setShowSuggestions(false);
      setSelectedSuggestionIndex(-1);
    }
  };

  /**
   * 开始打招呼任务
   */
  const handleStartAutomation = async () => {
    try {
      const targetCount = maxContacts === '' ? 10 : maxContacts;
      if (targetCount < 1 || targetCount > 500) {
        toast.error('打招呼数量必须在 1-500 之间');
        return;
      }

      // 自动将输入框中未点击"添加"的内容纳入期望职位列表
      let finalPositions = [...expectedPositions];
      const trimmedInput = positionInput.trim();
      if (trimmedInput && !finalPositions.includes(trimmedInput)) {
        finalPositions.push(trimmedInput);
        setExpectedPositions(finalPositions);
        setPositionInput('');
      }

      toast.info('正在启动打招呼任务...');

      const response = await fetch('/api/greeting/start', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          target_count: targetCount,
          expected_positions: finalPositions
        })
      });

      const data = await response.json();

      if (response.ok) {
        toast.success('打招呼任务已启动！');
        setGreetingStarted(true);
        startPolling();
      } else {
        let errorMessage = '启动失败';
        if (typeof data.detail === 'string') {
          errorMessage = data.detail;
        } else if (Array.isArray(data.detail)) {
          errorMessage = data.detail.map((err: any) => err.msg).join(', ');
        } else if (data.message) {
          errorMessage = data.message;
        }
        toast.error(errorMessage);
      }
    } catch (error) {
      console.error('启动任务失败:', error);
      toast.error('启动失败，请检查后端服务');
    }
  };

  /**
   * 保存为模板
   */
  const handleSaveTemplate = async () => {
    if (!templateName.trim()) {
      toast.error('请输入模板名称');
      return;
    }

    setSavingTemplate(true);
    try {
      await createTemplate({
        name: templateName.trim(),
        description: templateDescription.trim() || undefined,
        account_id: userInfo?.comId,
        headless: !showBrowser,
        greeting_count: maxContacts === '' ? 10 : maxContacts,
        expected_positions: expectedPositions.length > 0 ? expectedPositions : undefined,
      });

      toast.success('模板保存成功！');
      setSaveTemplateDialogOpen(false);
      setTemplateName('');
      setTemplateDescription('');
    } catch (error) {
      console.error('保存模板失败:', error);
      toast.error(error instanceof Error ? error.message : '保存模板失败');
    } finally {
      setSavingTemplate(false);
    }
  };

  /**
   * 开始轮询打招呼状态
   */
  const startPolling = () => {
    if (pollingIntervalRef.current) {
      clearInterval(pollingIntervalRef.current);
    }

    pollingIntervalRef.current = setInterval(async () => {
      try {
        const statusRes = await fetch('/api/greeting/status');
        const statusData = await statusRes.json();
        setGreetingStatus(statusData);

        const logsRes = await fetch('/api/greeting/logs?last_n=100');
        const logsData = await logsRes.json();
        setGreetingLogs(logsData.logs);

        if (statusData.status !== 'running') {
          if (pollingIntervalRef.current) {
            clearInterval(pollingIntervalRef.current);
            pollingIntervalRef.current = null;
          }
          setGreetingStarted(false);
        }
      } catch (error) {
        console.error('轮询失败:', error);
      }
    }, 1000);
  };

  // 清理轮询
  useEffect(() => {
    return () => {
      if (pollingIntervalRef.current) {
        clearInterval(pollingIntervalRef.current);
      }
      if (readyPollingRef.current) {
        clearInterval(readyPollingRef.current);
      }
      if (searchTimerRef.current) {
        clearTimeout(searchTimerRef.current);
      }
    };
  }, []);

  // 点击外部关闭下拉
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (
        suggestionsRef.current &&
        !suggestionsRef.current.contains(e.target as Node) &&
        inputRef.current &&
        !inputRef.current.contains(e.target as Node)
      ) {
        setShowSuggestions(false);
        setSelectedSuggestionIndex(-1);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // 从模板加载配置
  useEffect(() => {
    const templateData = sessionStorage.getItem('selectedTemplate');
    if (templateData) {
      try {
        const template = JSON.parse(templateData);

        setShowBrowser(!template.headless);
        if (template.greeting_count) {
          setMaxContacts(template.greeting_count);
        }
        if (template.expected_positions && template.expected_positions.length > 0) {
          setExpectedPositions(template.expected_positions);
        }

        sessionStorage.removeItem('selectedTemplate');
        toast.success(`已加载模板：${template.name}`);
      } catch (error) {
        console.error('加载模板失败:', error);
        toast.error('加载模板失败');
      }
    }
  }, []);

  /**
   * 渲染步骤1: 启动浏览器
   */
  const renderBrowserStep = () => (
    <Card className="max-w-2xl mx-auto">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Monitor className="h-6 w-6 text-primary" />
          步骤 1: 启动浏览器
        </CardTitle>
        <CardDescription>
          选择账号并启动浏览器，用于后续手动操作
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-4">
          {/* 账号选择 */}
          {accountsLoaded && availableAccounts.length > 0 && (
            <div className="space-y-2">
              <Label htmlFor="account-select">选择已保存的账号（可选）</Label>
              <Select
                value={selectedComId}
                onValueChange={(value) => setSelectedComId(value)}
              >
                <SelectTrigger id="account-select">
                  <SelectValue placeholder="不使用已有账号（全新登录）" />
                </SelectTrigger>
                <SelectContent>
                  {availableAccounts.map((account) => (
                    <SelectItem key={account.id} value={account.com_id.toString()}>
                      <div className="flex items-center gap-2">
                        {account.avatar && (
                          <img
                            src={account.avatar}
                            alt={account.show_name}
                            className="w-5 h-5 rounded-full"
                          />
                        )}
                        <span>{account.show_name}</span>
                        <span className="text-muted-foreground text-xs">
                          ({account.company_short_name || account.company_name})
                        </span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                {selectedComId
                  ? '将加载该账号的登录状态（cookies），可能免去手动登录'
                  : '不选择账号则需要在浏览器中手动登录'}
              </p>

              {selectedComId && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-xs text-muted-foreground"
                  onClick={() => setSelectedComId('')}
                >
                  清除选择
                </Button>
              )}

              <Separator />
            </div>
          )}

          <div className="flex items-start space-x-3 p-4 border rounded-lg">
            <Checkbox
              id="showBrowser"
              checked={showBrowser}
              onCheckedChange={(checked) => setShowBrowser(checked as boolean)}
            />
            <div className="flex-1">
              <Label
                htmlFor="showBrowser"
                className="text-sm font-medium leading-none cursor-pointer"
              >
                显示浏览器窗口
              </Label>
              <p className="text-sm text-muted-foreground mt-1.5">
                需要在浏览器中手动操作，建议保持勾选。
              </p>
            </div>
          </div>

          <div className="bg-blue-50 dark:bg-blue-950 p-4 rounded-lg">
            <h4 className="font-medium text-blue-900 dark:text-blue-100 mb-2">
              流程说明
            </h4>
            <ul className="text-sm text-blue-800 dark:text-blue-200 space-y-1">
              {selectedComId ? (
                <>
                  <li>1. 启动浏览器并自动加载账号 cookies</li>
                  <li>2. 如果 cookies 有效则自动登录，否则需手动登录</li>
                </>
              ) : (
                <li>1. 启动浏览器后，在浏览器中手动完成登录</li>
              )}
              <li>{selectedComId ? '3' : '2'}. 手动进入"推荐牛人"页面、选择职位、设置筛选条件</li>
              <li>{selectedComId ? '4' : '3'}. 程序检测到就绪后，配置打招呼数量并启动</li>
            </ul>
          </div>
        </div>

        <Button
          onClick={handleInitBrowser}
          disabled={browserInitializing}
          className="w-full"
          size="lg"
        >
          {browserInitializing ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              正在启动浏览器...
            </>
          ) : (
            <>
              <Zap className="mr-2 h-4 w-4" />
              启动浏览器
            </>
          )}
        </Button>
      </CardContent>
    </Card>
  );

  /**
   * 渲染步骤2: 手动操作引导
   */
  const renderManualStep = () => {
    const allReady = readyState.logged_in && readyState.on_recommend_page && readyState.has_frame;

    return (
      <Card className="max-w-2xl mx-auto">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Hand className="h-6 w-6 text-primary" />
            步骤 2: 在浏览器中完成操作
          </CardTitle>
          <CardDescription>
            请在已打开的浏览器窗口中完成以下操作，程序会自动检测状态
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* 安全验证提示 */}
          {readyState.needs_verification && (
            <div className="bg-red-50 dark:bg-red-950 p-4 rounded-lg border border-red-200 dark:border-red-800">
              <h4 className="font-medium text-red-900 dark:text-red-100 mb-2">
                需要完成安全验证
              </h4>
              <p className="text-sm text-red-800 dark:text-red-200">
                浏览器中出现了滑块验证或安全检查，请在浏览器中手动完成验证后继续。
              </p>
            </div>
          )}

          {/* 操作指引 */}
          <div className="bg-amber-50 dark:bg-amber-950 p-4 rounded-lg">
            <h4 className="font-medium text-amber-900 dark:text-amber-100 mb-3">
              请在浏览器中完成以下操作：
            </h4>
            <ol className="text-sm text-amber-800 dark:text-amber-200 space-y-2 list-decimal list-inside">
              <li>登录 Boss 直聘账号（如遇到验证滑块，请先完成验证）</li>
              <li>进入"推荐牛人"页面</li>
              <li>选择招聘职位</li>
              <li>设置筛选条件（如需要）</li>
            </ol>
          </div>

          {/* 状态检测指示器 */}
          <div className="space-y-3">
            <h4 className="font-medium text-sm text-muted-foreground">实时状态检测</h4>

            <div className="space-y-2">
              {/* 登录状态 */}
              <div className={`flex items-center gap-3 p-3 rounded-lg border ${
                readyState.logged_in
                  ? 'bg-green-50 border-green-200 dark:bg-green-950 dark:border-green-800'
                  : 'bg-gray-50 border-gray-200 dark:bg-gray-900 dark:border-gray-700'
              }`}>
                <div className={`flex items-center justify-center w-6 h-6 rounded-full ${
                  readyState.logged_in
                    ? 'bg-green-500 text-white'
                    : 'bg-gray-300 dark:bg-gray-600'
                }`}>
                  {readyState.logged_in ? (
                    <CheckCircle2 className="h-4 w-4" />
                  ) : (
                    <span className="text-xs font-medium">1</span>
                  )}
                </div>
                <div className="flex-1">
                  <p className={`text-sm font-medium ${
                    readyState.logged_in ? 'text-green-700 dark:text-green-300' : ''
                  }`}>
                    {readyState.logged_in ? '已登录' : '等待登录...'}
                  </p>
                  {readyState.logged_in && userInfo?.showName && (
                    <p className="text-xs text-green-600 dark:text-green-400">
                      {userInfo.showName}
                    </p>
                  )}
                </div>
                {readyState.logged_in && (
                  <Badge variant="outline" className="bg-green-100 text-green-700 border-green-300">
                    完成
                  </Badge>
                )}
              </div>

              {/* 推荐页面状态 */}
              <div className={`flex items-center gap-3 p-3 rounded-lg border ${
                readyState.on_recommend_page
                  ? 'bg-green-50 border-green-200 dark:bg-green-950 dark:border-green-800'
                  : 'bg-gray-50 border-gray-200 dark:bg-gray-900 dark:border-gray-700'
              }`}>
                <div className={`flex items-center justify-center w-6 h-6 rounded-full ${
                  readyState.on_recommend_page
                    ? 'bg-green-500 text-white'
                    : 'bg-gray-300 dark:bg-gray-600'
                }`}>
                  {readyState.on_recommend_page ? (
                    <CheckCircle2 className="h-4 w-4" />
                  ) : (
                    <span className="text-xs font-medium">2</span>
                  )}
                </div>
                <div className="flex-1">
                  <p className={`text-sm font-medium ${
                    readyState.on_recommend_page ? 'text-green-700 dark:text-green-300' : ''
                  }`}>
                    {readyState.on_recommend_page ? '在推荐牛人页面' : '等待进入推荐牛人页面...'}
                  </p>
                </div>
                {readyState.on_recommend_page && (
                  <Badge variant="outline" className="bg-green-100 text-green-700 border-green-300">
                    完成
                  </Badge>
                )}
              </div>

              {/* 推荐列表状态 */}
              <div className={`flex items-center gap-3 p-3 rounded-lg border ${
                readyState.has_frame
                  ? 'bg-green-50 border-green-200 dark:bg-green-950 dark:border-green-800'
                  : 'bg-gray-50 border-gray-200 dark:bg-gray-900 dark:border-gray-700'
              }`}>
                <div className={`flex items-center justify-center w-6 h-6 rounded-full ${
                  readyState.has_frame
                    ? 'bg-green-500 text-white'
                    : 'bg-gray-300 dark:bg-gray-600'
                }`}>
                  {readyState.has_frame ? (
                    <CheckCircle2 className="h-4 w-4" />
                  ) : (
                    <span className="text-xs font-medium">3</span>
                  )}
                </div>
                <div className="flex-1">
                  <p className={`text-sm font-medium ${
                    readyState.has_frame ? 'text-green-700 dark:text-green-300' : ''
                  }`}>
                    {readyState.has_frame ? '推荐列表已加载' : '等待推荐列表加载...'}
                  </p>
                </div>
                {readyState.has_frame && (
                  <Badge variant="outline" className="bg-green-100 text-green-700 border-green-300">
                    完成
                  </Badge>
                )}
              </div>
            </div>

            {/* 轮询状态提示 */}
            {!allReady && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                每 3 秒自动检测一次...
              </div>
            )}
          </div>

          {/* 按钮组 */}
          <div className="flex gap-3 justify-between">
            <Button
              variant="outline"
              onClick={() => {
                if (readyPollingRef.current) {
                  clearInterval(readyPollingRef.current);
                }
                setCurrentStep('browser');
              }}
            >
              ← 返回
            </Button>
            <div className="flex gap-2">
              <Button
                variant="outline"
                onClick={() => {
                  if (readyPollingRef.current) {
                    clearInterval(readyPollingRef.current);
                  }
                  startReadyStatePolling();
                }}
              >
                🔄 手动刷新
              </Button>
              <Button
                onClick={() => {
                  if (readyPollingRef.current) {
                    clearInterval(readyPollingRef.current);
                  }
                  setCurrentStep('confirm');
                }}
                disabled={!allReady}
                size="lg"
              >
                {allReady ? (
                  <>
                    <CheckCircle2 className="mr-2 h-4 w-4" />
                    继续配置
                  </>
                ) : (
                  '请先完成上述操作'
                )}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    );
  };

  /**
   * 渲染步骤3: 配置并启动
   */
  const renderConfirmStep = () => {
    return (
      <div className="max-w-2xl mx-auto space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <PlayCircle className="h-6 w-6 text-primary" />
              步骤 3: 配置并启动
            </CardTitle>
            <CardDescription>
              设置打招呼参数，然后启动自动打招呼
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            {/* 用户信息 */}
            {userInfo && (
              <div className="flex items-center gap-4 p-4 bg-green-50 dark:bg-green-950 rounded-lg">
                {userInfo.avatar && (
                  <img
                    src={userInfo.avatar}
                    alt={userInfo.showName}
                    className="w-10 h-10 rounded-full object-cover"
                  />
                )}
                <div>
                  <p className="font-medium">{userInfo.showName || '已登录用户'}</p>
                  <p className="text-sm text-muted-foreground">
                    已就绪，职位和筛选条件已在浏览器中手动设置
                  </p>
                </div>
              </div>
            )}

            <div className="space-y-4">
              {/* 打招呼数量 */}
              <div className="space-y-2">
                <Label htmlFor="maxContacts">打招呼数量</Label>
                <p className="text-sm text-muted-foreground">
                  成功打招呼达到此数量后停止（不包括跳过的候选人）
                </p>
                <Input
                  id="maxContacts"
                  type="number"
                  min="1"
                  max="500"
                  value={maxContacts}
                  onChange={(e) => {
                    const value = e.target.value;
                    if (value === '') {
                      setMaxContacts('');
                    } else {
                      const num = parseInt(value);
                      if (!isNaN(num)) {
                        setMaxContacts(num);
                      }
                    }
                  }}
                  onBlur={(e) => {
                    if (e.target.value === '') {
                      setMaxContacts(10);
                    }
                  }}
                  className="max-w-xs"
                />
                <p className="text-sm text-muted-foreground">
                  最多可设置 500 人，建议分批次进行，避免触发平台限制
                </p>
              </div>

              {/* 期望职位匹配 */}
              <div className="space-y-2">
                <Label>期望职位匹配（可选）</Label>
                <p className="text-sm text-muted-foreground">
                  只向期望职位包含以下关键词的候选人打招呼，支持搜索历史关键词
                </p>

                <div className="relative">
                  <div className="flex gap-2">
                    <Input
                      ref={inputRef}
                      placeholder="输入职位关键词，如：Java、产品经理"
                      value={positionInput}
                      onChange={(e) => handlePositionInputChange(e.target.value)}
                      onKeyDown={handlePositionKeyDown}
                      onFocus={() => {
                        // 聚焦时加载历史关键词
                        if (keywordSuggestions.length > 0) {
                          setShowSuggestions(true);
                        } else {
                          handlePositionInputChange(positionInput);
                        }
                      }}
                      className="flex-1"
                    />
                    <Button
                      type="button"
                      onClick={() => handleAddPosition()}
                      variant="outline"
                    >
                      添加
                    </Button>
                  </div>

                  {/* 搜索建议下拉列表 */}
                  {showSuggestions && (() => {
                    const filteredSuggestions = keywordSuggestions.filter(
                      k => !expectedPositions.includes(k.name)
                    );
                    const trimmed = positionInput.trim();
                    const hasCreateOption = trimmed && !filteredSuggestions.some(k => k.name === trimmed);
                    const hasItems = filteredSuggestions.length > 0 || hasCreateOption;

                    if (!hasItems) return null;

                    return (
                      <div
                        ref={suggestionsRef}
                        className="absolute z-50 top-full left-0 right-12 mt-1 bg-popover border rounded-md shadow-md max-h-48 overflow-y-auto"
                      >
                        {filteredSuggestions.map((keyword, index) => (
                          <div
                            key={keyword.id}
                            className={`flex items-center justify-between px-3 py-2 text-sm cursor-pointer hover:bg-accent ${
                              selectedSuggestionIndex === index ? 'bg-accent' : ''
                            }`}
                            onClick={() => handleAddPosition(keyword.name)}
                          >
                            <span>{keyword.name}</span>
                            <div className="flex items-center gap-2">
                              <span className="text-xs text-muted-foreground">
                                使用 {keyword.usage_count} 次
                              </span>
                              <X
                                className="h-3 w-3 text-muted-foreground hover:text-red-600"
                                onClick={(e) => handleDeleteKeyword(e, keyword.id)}
                              />
                            </div>
                          </div>
                        ))}
                        {hasCreateOption && (
                          <div
                            className={`flex items-center px-3 py-2 text-sm cursor-pointer hover:bg-accent border-t ${
                              selectedSuggestionIndex === filteredSuggestions.length ? 'bg-accent' : ''
                            }`}
                            onClick={() => handleAddPosition(trimmed)}
                          >
                            <span className="text-primary">
                              创建 "{trimmed}"
                            </span>
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </div>

                {expectedPositions.length > 0 && (
                  <div className="flex flex-wrap gap-2 mt-2">
                    {expectedPositions.map((pos, index) => (
                      <Badge
                        key={index}
                        variant="secondary"
                        className="px-3 py-1 text-sm"
                      >
                        {pos}
                        <X
                          className="ml-1 h-3 w-3 cursor-pointer hover:text-red-600"
                          onClick={() => handleRemovePosition(index)}
                        />
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="flex gap-4">
              <Button
                variant="outline"
                onClick={() => {
                  setCurrentStep('manual');
                  startReadyStatePolling();
                }}
                disabled={greetingStarted}
              >
                返回
              </Button>
              <Button
                variant="outline"
                onClick={() => setSaveTemplateDialogOpen(true)}
                disabled={greetingStarted}
              >
                <Save className="mr-2 h-4 w-4" />
                保存为模板
              </Button>
              <Button
                onClick={handleStartAutomation}
                disabled={greetingStarted}
                className="flex-1"
              >
                {greetingStarted ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    任务进行中...
                  </>
                ) : (
                  <>
                    <PlayCircle className="mr-2 h-4 w-4" />
                    开始打招呼
                  </>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* 进度显示 */}
        {greetingStarted && greetingStatus && (
          <>
            <Card>
              <CardHeader>
                <CardTitle>执行进度</CardTitle>
                <CardDescription>
                  {greetingStatus.progress?.toFixed(1)}% 完成
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="w-full bg-gray-200 rounded-full h-2.5">
                  <div
                    className="bg-blue-600 h-2.5 rounded-full transition-all"
                    style={{ width: `${greetingStatus.progress || 0}%` }}
                  ></div>
                </div>
                <p className="text-sm text-muted-foreground mt-2">
                  {greetingStatus.status === 'running' && `正在处理第 ${greetingStatus.current_index} 个候选人...`}
                  {greetingStatus.status === 'completed' && `任务完成！成功 ${greetingStatus.success_count} 个，失败 ${greetingStatus.failed_count} 个${greetingStatus.skipped_count > 0 ? `，跳过 ${greetingStatus.skipped_count} 个` : ''}`}
                  {greetingStatus.status === 'idle' && '等待开始...'}
                </p>

                <div className="grid grid-cols-3 gap-4 mt-4">
                  <div>
                    <p className="text-sm text-muted-foreground">成功数</p>
                    <p className="text-2xl font-bold text-green-600">{greetingStatus.success_count}</p>
                  </div>
                  <div>
                    <p className="text-sm text-muted-foreground">失败数</p>
                    <p className="text-2xl font-bold text-red-600">{greetingStatus.failed_count}</p>
                  </div>
                  <div>
                    <p className="text-sm text-muted-foreground">跳过数</p>
                    <p className="text-2xl font-bold text-yellow-600">{greetingStatus.skipped_count || 0}</p>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* 日志显示 */}
            <Card>
              <CardHeader>
                <CardTitle>运行日志</CardTitle>
                <CardDescription>实时显示任务执行日志</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="h-96 overflow-y-auto bg-gray-50 rounded-lg p-4 font-mono text-sm space-y-1">
                  {greetingLogs.length === 0 ? (
                    <p className="text-muted-foreground text-center py-8">
                      暂无日志
                    </p>
                  ) : (
                    greetingLogs.map((log, index) => (
                      <div
                        key={index}
                        className="flex items-start gap-2 py-1 border-b border-gray-200 last:border-0"
                      >
                        <span className="text-xs text-gray-500 w-24 flex-shrink-0">
                          {new Date(log.timestamp).toLocaleTimeString()}
                        </span>
                        <span className="flex-1">{log.message}</span>
                      </div>
                    ))
                  )}
                </div>
              </CardContent>
            </Card>
          </>
        )}

        {/* 保存模板对话框 */}
        <Dialog open={saveTemplateDialogOpen} onOpenChange={setSaveTemplateDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>保存为模板</DialogTitle>
              <DialogDescription>
                保存当前配置为模板，下次可以快速复用
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label htmlFor="template-name">模板名称 *</Label>
                <Input
                  id="template-name"
                  placeholder="如：Java开发-活跃候选人"
                  value={templateName}
                  onChange={(e) => setTemplateName(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="template-description">模板描述（可选）</Label>
                <Textarea
                  id="template-description"
                  placeholder="描述此模板的用途或特点..."
                  value={templateDescription}
                  onChange={(e) => setTemplateDescription(e.target.value)}
                  rows={3}
                />
              </div>
              <div className="text-sm text-muted-foreground">
                <p>将保存以下配置：</p>
                <ul className="list-disc list-inside mt-2 space-y-1">
                  <li>打招呼数量：{maxContacts === '' ? 10 : maxContacts}</li>
                  {expectedPositions.length > 0 && (
                    <li>期望职位：{expectedPositions.join('、')}</li>
                  )}
                </ul>
              </div>
            </div>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => {
                  setSaveTemplateDialogOpen(false);
                  setTemplateName('');
                  setTemplateDescription('');
                }}
                disabled={savingTemplate}
              >
                取消
              </Button>
              <Button
                onClick={handleSaveTemplate}
                disabled={savingTemplate}
              >
                {savingTemplate ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    保存中...
                  </>
                ) : (
                  <>
                    <Save className="mr-2 h-4 w-4" />
                    保存模板
                  </>
                )}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Zap className="h-8 w-8 text-primary" />
          <div>
            <h1 className="text-3xl font-bold tracking-tight">自动化向导</h1>
            <p className="text-muted-foreground">
              快速配置并启动自动化招聘任务
            </p>
          </div>
        </div>
      </div>

      {/* 步骤指示器 - 3步 */}
      <div className="flex items-center justify-center gap-3 py-6">
        {/* 步骤1: 启动浏览器 */}
        <div className="flex items-center gap-2">
          <div
            className={`flex items-center justify-center w-10 h-10 rounded-full border-2 ${
              currentStep === 'browser'
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-blue-50 text-blue-700 border-blue-700'
            }`}
          >
            {currentStep !== 'browser' ? <CheckCircle2 className="h-5 w-5" /> : '1'}
          </div>
          <span
            className={`font-medium text-sm ${
              currentStep === 'browser' ? 'text-primary' : 'text-muted-foreground'
            }`}
          >
            启动浏览器
          </span>
        </div>

        <div className="w-16 h-0.5 bg-muted" />

        {/* 步骤2: 手动操作 */}
        <div className="flex items-center gap-2">
          <div
            className={`flex items-center justify-center w-10 h-10 rounded-full border-2 ${
              currentStep === 'manual'
                ? 'bg-primary text-primary-foreground border-primary'
                : currentStep === 'confirm'
                ? 'bg-blue-50 text-blue-700 border-blue-700'
                : 'border-muted-foreground text-muted-foreground'
            }`}
          >
            {currentStep === 'confirm' ? <CheckCircle2 className="h-5 w-5" /> : '2'}
          </div>
          <span
            className={`font-medium text-sm ${
              currentStep === 'manual' ? 'text-primary' : 'text-muted-foreground'
            }`}
          >
            手动操作
          </span>
        </div>

        <div className="w-16 h-0.5 bg-muted" />

        {/* 步骤3: 配置并启动 */}
        <div className="flex items-center gap-2">
          <div
            className={`flex items-center justify-center w-10 h-10 rounded-full border-2 ${
              currentStep === 'confirm'
                ? 'bg-primary text-primary-foreground border-primary'
                : 'border-muted-foreground text-muted-foreground'
            }`}
          >
            3
          </div>
          <span
            className={`font-medium text-sm ${
              currentStep === 'confirm' ? 'text-primary' : 'text-muted-foreground'
            }`}
          >
            配置启动
          </span>
        </div>
      </div>

      {/* 步骤内容 */}
      {currentStep === 'browser' && renderBrowserStep()}
      {currentStep === 'manual' && renderManualStep()}
      {currentStep === 'confirm' && renderConfirmStep()}
    </div>
  );
}
