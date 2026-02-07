/**
 * 期望职位关键词管理 Hook
 */
import { useCallback } from 'react';
import { get, del } from '@/lib/api';

export interface PositionKeyword {
  id: number;
  name: string;
  usage_count: number;
  created_at: string;
}

export function usePositionKeywords() {
  const searchKeywords = useCallback(async (query: string): Promise<PositionKeyword[]> => {
    return await get<PositionKeyword[]>(`/position-keywords?q=${encodeURIComponent(query)}`);
  }, []);

  const deleteKeyword = useCallback(async (id: number): Promise<void> => {
    await del(`/position-keywords/${id}`);
  }, []);

  return { searchKeywords, deleteKeyword };
}
