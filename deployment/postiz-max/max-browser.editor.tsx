'use client';
import { PostComment, withProvider } from '@gitroom/frontend/components/new-launch/providers/high.order.provider';
export default withProvider({ postComment: PostComment.POST, comments: false,
  minimumCharacters: [], SettingsComponent: null, dto: undefined, maximumCharacters: 3500 });
