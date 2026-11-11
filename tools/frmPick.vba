' Code of the UserForm "frmPick" (checkbox popup). Paste it in the form's code window.
' The form itself stays EMPTY in the designer: every control is created below at
' runtime, so you only have to insert a blank UserForm and name it frmPick.

Option Explicit

Private WithEvents btnOK As MSForms.CommandButton
Private WithEvents btnCancel As MSForms.CommandButton
Private WithEvents btnClear As MSForms.CommandButton
Private lst As MSForms.ListBox

Public Result As String
Public Confirmed As Boolean

Private Sub UserForm_Initialize()
    Me.Caption = "Choisir une ou plusieurs valeurs"
    Me.Width = 290
    Me.Height = 440

    Set lst = Me.Controls.Add("Forms.ListBox.1", "lstItems", True)
    With lst
        .Left = 10
        .Top = 10
        .Width = 258
        .Height = 340
        .ListStyle = fmListStyleOption
        .MultiSelect = fmMultiSelectMulti
    End With

    Set btnClear = Me.Controls.Add("Forms.CommandButton.1", "btnClear", True)
    With btnClear
        .Caption = "Tout d" & ChrW(233) & "cocher"
        .Left = 10
        .Top = 358
        .Width = 100
        .Height = 24
    End With

    Set btnOK = Me.Controls.Add("Forms.CommandButton.1", "btnOK", True)
    With btnOK
        .Caption = "OK"
        .Left = 120
        .Top = 358
        .Width = 70
        .Height = 24
        .Default = True
    End With

    Set btnCancel = Me.Controls.Add("Forms.CommandButton.1", "btnCancel", True)
    With btnCancel
        .Caption = "Annuler"
        .Left = 198
        .Top = 358
        .Width = 70
        .Height = 24
        .Cancel = True
    End With
End Sub

Public Sub Setup(ByVal title As String, ByVal src As Range, ByVal current As String)
    Dim c As Range, parts() As String, i As Long, j As Long
    Me.Caption = title
    lst.Clear
    For Each c In src.Cells
        If Len(CStr(c.Value)) > 0 Then lst.AddItem CStr(c.Value)
    Next c
    If Len(Trim$(current)) > 0 Then
        parts = Split(current, ",")
        For i = LBound(parts) To UBound(parts)
            For j = 0 To lst.ListCount - 1
                If StrComp(Trim$(parts(i)), lst.List(j), vbTextCompare) = 0 Then lst.Selected(j) = True
            Next j
        Next i
    End If
End Sub

Private Sub btnClear_Click()
    Dim i As Long
    For i = 0 To lst.ListCount - 1
        lst.Selected(i) = False
    Next i
End Sub

Private Sub btnOK_Click()
    Dim i As Long, s As String
    Result = ""
    For i = 0 To lst.ListCount - 1
        If lst.Selected(i) Then
            If Left$(lst.List(i), 7) = "[Toutes" Then
                Result = lst.List(i)          ' "all" replaces everything else
                GoTo Finish
            End If
            If Len(s) > 0 Then s = s & ", "
            s = s & lst.List(i)
        End If
    Next i
    Result = s
Finish:
    Confirmed = True
    Me.Hide
End Sub

Private Sub btnCancel_Click()
    Confirmed = False
    Me.Hide
End Sub
