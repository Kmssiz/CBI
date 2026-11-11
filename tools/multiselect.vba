' Multi-select for the "Reports" sheet. Goes in the "ThisWorkbook" code module.
' REPLACE the previous content of ThisWorkbook with this file.
' No names are hard-coded: it works for any column whose header starts with Soc, Dir or Mod.
'
'  1) Double-click a Societe(s) / Direction(s) / Module(s) cell: a checkbox popup (form frmPick,
'     see tools/frmPick.vba) lists every option; tick several, press OK.
'  2) Or use the dropdown arrow: each pick is ADDED to the cell ("A, B"); picking a value
'     already present REMOVES it.
' "[Toutes ...]" replaces everything; picking a specific societe drops it.

Option Explicit

Private lastAddr As String
Private lastVal As String

Private Sub Workbook_SheetSelectionChange(ByVal Sh As Object, ByVal Target As Range)
    ' Remember the value of the selected cell: it is the "old value" when the next pick arrives.
    If Sh.Name <> "Reports" Then Exit Sub
    If Target.Cells.Count <> 1 Then
        lastAddr = ""
        Exit Sub
    End If
    lastAddr = Target.Address
    lastVal = CStr(Target.Value)
End Sub

Private Sub Workbook_SheetBeforeDoubleClick(ByVal Sh As Object, ByVal Target As Range, Cancel As Boolean)
    Dim hdr As String, src As Range, f As frmPick

    If Sh.Name <> "Reports" Then Exit Sub
    If Target.Cells.Count <> 1 Then Exit Sub
    If Target.Row < 2 Then Exit Sub
    hdr = CStr(Sh.Cells(1, Target.Column).Value)
    Select Case Left$(hdr, 3)
        Case "Soc", "Dir", "Mod"
        Case Else: Exit Sub
    End Select

    Cancel = True                                   ' do not enter cell edit mode
    On Error GoTo Fail
    Set src = Sh.Evaluate(Mid$(Target.Validation.Formula1, 2))   ' the dropdown's own list
    Set f = New frmPick
    f.Setup hdr, src, CStr(Target.Value)
    f.Show
    If f.Confirmed Then
        Application.EnableEvents = False
        If Len(f.Result) = 0 Then Target.ClearContents Else Target.Value = f.Result
        If Target.Address = lastAddr Then lastVal = CStr(Target.Value)
    End If

Fail:
    Application.EnableEvents = True
    If Not f Is Nothing Then Unload f
    If Err.Number <> 0 Then MsgBox "Impossible d'ouvrir la liste : " & Err.Description, vbExclamation
End Sub

Private Sub Workbook_SheetChange(ByVal Sh As Object, ByVal Target As Range)
    Dim hdr As String, newVal As String, oldVal As String
    Dim parts() As String, outList As String, p As String
    Dim i As Long, found As Boolean, isAll As Boolean

    If Sh.Name <> "Reports" Then Exit Sub
    If Target.Cells.Count <> 1 Then Exit Sub
    If Target.Row < 2 Then Exit Sub
    hdr = CStr(Sh.Cells(1, Target.Column).Value)
    Select Case Left$(hdr, 3)
        Case "Soc", "Dir", "Mod"
        Case Else: Exit Sub
    End Select

    On Error GoTo CleanUp
    Application.EnableEvents = False

    newVal = Trim$(CStr(Target.Value))
    If Target.Address = lastAddr Then oldVal = Trim$(lastVal) Else oldVal = ""

    ' Delete / manual multi-value paste: leave the cell as it is.
    If Len(newVal) = 0 Or InStr(newVal, ",") > 0 Then GoTo Done

    isAll = (Left$(newVal, 7) = "[Toutes")
    outList = ""
    found = False
    If Len(oldVal) > 0 Then
        parts = Split(oldVal, ",")
        For i = LBound(parts) To UBound(parts)
            p = Trim$(parts(i))
            If Len(p) > 0 Then
                If StrComp(p, newVal, vbTextCompare) = 0 Then
                    found = True                                   ' picked again -> remove
                ElseIf (Not isAll) And Left$(p, 7) = "[Toutes" Then
                    ' a specific pick replaces "all"
                Else
                    If Len(outList) > 0 Then outList = outList & ", "
                    outList = outList & p
                End If
            End If
        Next i
    End If
    If Not found Then
        If isAll Then
            outList = newVal
        Else
            If Len(outList) > 0 Then outList = outList & ", "
            outList = outList & newVal
        End If
    End If

    If Len(outList) = 0 Then Target.ClearContents Else Target.Value = outList

Done:
    If Target.Address = lastAddr Then lastVal = CStr(Target.Value)
CleanUp:
    Application.EnableEvents = True
End Sub
