# Sidebar Git review

Panel Changes được sắp xếp theo thứ tự đọc: branch và worktree, commit message, thao tác stage, file thay đổi rồi commit gần đây. Một vùng cuộn chung giúp truy cập file và lịch sử trên cửa sổ nhỏ. Giao diện sáng/tối dùng palette chung.

**Stage all** stage các đường dẫn unstaged và untracked đang liệt kê của worktree được chọn. **Unstage all** bỏ các đường dẫn staged đang liệt kê khỏi index của worktree đó, giữ file làm việc. Cả hai gọi API hiện có với đường dẫn tường minh, có xử lý rename; không chạy lệnh stage toàn repository. Backend nhận 1–1000 file đã thay đổi mỗi thao tác và từ chối lựa chọn đã cũ. Nút cộng/trừ từng file vẫn nằm bên phải mỗi dòng.

Status hai cột của Git phân biệt index và working tree. File stage một phần được tính trong cả tổng staged lẫn unstaged. File đã stage deletion vẫn review được.

Commit message nằm trên danh sách thay đổi. Composer ghi rõ **Commit staged changes**. Nút này commit toàn bộ index hiện tại, kể cả file đã stage ngoài Buddy, với message người dùng nhập; không tự stage file. Edit chưa stage và file untracked vẫn ở trên đĩa. Identity, hook và signing Git đã cấu hình vẫn áp dụng; lỗi hiển thị cho người dùng. Workflow này không push, bypass hook, amend hay discard file. Khi thành công, composer được xóa và Git status/history refresh. Lỗi commit/stage của workspace trước không được hiển thị lên workspace khác vừa chọn.

Header **CHANGES** hiển thị số file và cho mở/thu danh sách. Mỗi dòng có tên file nổi bật, thư mục hoặc nguồn rename màu nhạt, thống kê dòng nếu có, trạng thái Git và nút stage. Nhấp dòng mở working diff hiện có trong preview trung tâm. Commit gần đây vẫn mở rộng danh sách file; chọn file lịch sử mở diff của commit đó. Sidebar hiện không thêm push/pull/PR từ xa hay diff tổng hợp “View all”.

Commit thường và merge commit so với first parent; initial commit so với cây rỗng. Patch gồm source/destination path của rename. Thay đổi binary hiển thị summary Git thay vì nội dung text giả.

Draft commit message được giữ riêng theo project/worktree đã đăng ký khi panel còn mount; đổi worktree không mang draft sang index khác. Preview file/history bất đồng bộ bỏ qua reply của workspace trước hoặc selection đã bị thay thế.

## Thống kê dòng

Trường tùy chọn `GitFile.lineStats` chứa số dòng `added` và `removed`. Renderer không thay dữ liệu thiếu bằng số không.

- File tracked có HEAD dùng một phép tính numstat tổng hợp HEAD đến working tree, có rename. Không cộng máy móc staged và unstaged: chỉnh sửa bị đảo ngược ở working tree có thể có số thay đổi ròng bằng không thật.
- Trước commit đầu tiên, thay đổi chỉ staged có thể dùng baseline index ban đầu. Thay đổi kết hợp staged/working không có một baseline đã kiểm chứng sẽ giữ trạng thái chưa biết.
- File văn bản UTF-8 thường chưa tracked dùng số dòng hiện tại, tính cả dòng cuối không có newline. Đây là đếm văn bản cục bộ, không phải Git diff. Mỗi lần refresh chỉ kiểm tra 100 file untracked đầu tiên, tối đa 1 MiB mỗi file, concurrency bốn.
- Dữ liệu binary, thuộc tính `-diff` tường minh, symlink, file quá lớn/không đọc được, trạng thái không hỗ trợ/conflict và lỗi lấy thống kê giữ số dòng chưa biết khi áp dụng. Không đọc target của symlink để đếm dòng.

## Boundary và validation

Preload hữu hạn cung cấp `stageFiles(projectId, worktreePath, files)`, `unstageFiles(...)`, `commitStaged(projectId, worktreePath, message)`, `commitFiles(projectId, worktreePath, hash)`, `commitDiff(projectId, worktreePath, hash, file)`. Manager kiểm tra worktree thuộc project đã đăng ký trước khi truy cập Git. Mutation nhận 1–1000 file đang thay đổi được chọn tường minh, thêm original path khi rename, từ chối chọn cả directory nếu không có entry changed file tương ứng. Từ chối absolute path, traversal, NUL và segment `.git`. Git chạy bằng argv với `--literal-pathspecs` toàn cục và `--` trước path; tên như `:(glob)*` không thể mở rộng sang file khác.

Unstage dùng reset selected path theo HEAD. Với unborn branch, chỉ xóa entry index được chọn bằng `git rm --cached`; working file vẫn giữ nguyên. Commit message phải có 1–10.000 ký tự theo validation. Commit review nhận full hash hexadecimal 40/64 ký tự và kiểm tra file thuộc commit. Patch tắt external diff driver, text conversion và color. Giới hạn subprocess Git hiện có là 15 giây, output 4 MiB; diff lớn hoặc hook/signing chậm có thể báo lỗi. Git thay đổi từ ngoài đồng thời có thể làm selection hết hiệu lực; refresh và review trước khi thử lại.

`tests/git-review.test.ts` dùng repository tạm để kiểm tra literal path, giữ riêng file không chọn, initial commit/unstage, unstage rename đã stage và history, deletion preview, commit nội dung index trong khi giữ edit unstaged mới hơn. Test không stage hoặc commit repository của người dùng.

## Đối chiếu source và validation

Đã tham khảo Orca tại `right-sidebar/source-control/commit/commit-area.tsx`, `listing/uncommitted-sections.tsx`, `listing/section-header.tsx`, `listing/row-layout.ts`. Buddy dùng React component riêng và API Git cục bộ hiện có; đây là cải thiện khả năng đọc có phạm vi rõ ràng, chưa đạt toàn bộ source-control parity.

Validation gồm lint/build desktop, test backend thống kê dòng và smoke workflow Electron đóng gói. Fixture smoke stage/unstage file, chỉ commit README trong repository tạm, kiểm tra các thay đổi fixture khác còn unstaged và mở diff lịch sử vừa tạo. Xem [workspace UI](workspace-ui_vi.md) và [cài đặt appearance](settings_vi.md).


Implementation được viết mới sau khi tham khảo Orca tại `ba5f708290b72012132fb23a6f016b8fd5601718`: [commit diff](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-diff.ts), [staged commit context](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/staged-commit-context.ts), [commit changes](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-changes.ts). Full graph, hunk staging, merge tooling, remote operation và AI commit-message generation chưa thuộc slice này.
