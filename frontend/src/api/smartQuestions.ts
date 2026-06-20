/** 智能追问 API */
import api, { ApiResponse } from './client';

export interface SmartQuestion {
  question: string;
  hint: string;
  options: string[];
}

export interface SmartQuestionsResult {
  questions: SmartQuestion[];
  summary: string;
}

export async function generateSmartQuestions(userInput: string, previousAnswers?: Record<string, string>) {
  const res = await api.post<ApiResponse<SmartQuestionsResult>>('/smart-questions', {
    user_input: userInput,
    previous_answers: previousAnswers || {},
  });
  return res.data;
}
