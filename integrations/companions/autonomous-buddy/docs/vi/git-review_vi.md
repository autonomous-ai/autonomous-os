# Review Git local

Panel Changes hiển thị status hai cột của Git, số file staged/unstaged và thao tác Stage (+), Unstage (−) tường minh cho từng file. File stage một phần được tính ở cả hai nhóm. Click file đang thay đổi mở combined diff từ HEAD tới working tree bằng preview hiện có; file untracked dạng text được đọc để preview. Hàng rename giữ original path. Có thể review file đã stage deletion.

Composer ghi rõ **Commit staged changes**. Nút này commit toàn bộ index hiện tại, kể cả file đã stage ngoài Buddy, với message người dùng nhập; không tự stage file. Edit chưa stage và file untracked vẫn ở trên đĩa. Identity, hook và signing Git đã cấu hình vẫn áp dụng; lỗi hiển thị cho người dùng. Workflow này không push, bypass hook, amend hay discard file. Khi thành công, composer được xóa và Git status/history refresh.

Click recent commit để mở danh sách file thay đổi rồi chọn file xem patch. Commit thường và merge commit so với first parent; initial commit so với cây rỗng. Patch gồm source/destination path của rename. Thay đổi binary hiển thị summary Git thay vì nội dung text giả.

Draft commit message được giữ riêng theo project/worktree đã đăng ký khi panel còn mount; đổi worktree không mang draft sang index khác. Preview file/history bất đồng bộ bỏ qua reply của workspace trước hoặc selection đã bị thay thế.

## Boundary và validation

Preload hữu hạn cung cấp `stageFiles(projectId, worktreePath, files)`, `unstageFiles(...)`, `commitStaged(projectId, worktreePath, message)`, `commitFiles(projectId, worktreePath, hash)`, `commitDiff(projectId, worktreePath, hash, file)`. Manager kiểm tra worktree thuộc project đã đăng ký trước khi truy cập Git. Mutation nhận 1–1000 file đang thay đổi được chọn tường minh, thêm original path khi rename, từ chối chọn cả directory nếu không có entry changed file tương ứng. Từ chối absolute path, traversal, NUL và segment `.git`. Git chạy bằng argv với `--literal-pathspecs` toàn cục và `--` trước path; tên như `:(glob)*` không thể mở rộng sang file khác.

Unstage dùng reset selected path theo HEAD. Với unborn branch, chỉ xóa entry index được chọn bằng `git rm --cached`; working file vẫn giữ nguyên. Commit message phải có 1–10.000 ký tự theo validation. Commit review nhận full hash hexadecimal 40/64 ký tự và kiểm tra file thuộc commit. Patch tắt external diff driver, text conversion và color. Giới hạn subprocess Git hiện có là 15 giây, output 4 MiB; diff lớn hoặc hook/signing chậm có thể báo lỗi. Git thay đổi từ ngoài đồng thời có thể làm selection hết hiệu lực; refresh và review trước khi thử lại.

`tests/git-review.test.ts` dùng repository tạm để kiểm tra literal path, giữ riêng file không chọn, initial commit/unstage, unstage rename đã stage và history, deletion preview, commit nội dung index trong khi giữ edit unstaged mới hơn. Test không stage hoặc commit repository của người dùng.

## Audit nguồn

Implementation được viết mới sau khi tham khảo Orca tại `ba5f708290b72012132fb23a6f016b8fd5601718`: [commit diff](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-diff.ts), [staged commit context](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/staged-commit-context.ts), [commit changes](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-changes.ts). Full graph, hunk staging, merge tooling, remote operation và AI commit-message generation chưa thuộc slice này.
